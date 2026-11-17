#!/usr/bin/env python3
"""
langsmith_trace.py — optional LangSmith tracing for the Claude calls.

Every model call in this project funnels through `author.call_claude`, and
three operations wrap it: the authoring pass, the AI edit, and brand
inference. This module gives each of those a trace in LangSmith — a `chain`
run for the operation with an `llm` child for the request itself, carrying the
prompts, the reply, token usage and the model name.

Written against the LangSmith REST API directly (POST /runs, PATCH /runs/<id>)
rather than the `langsmith` SDK — a leftover of the project's retired
stdlib-only rule. It works; migrating to the SDK is its own change.

Three properties this must have, because it sits in the path of a paid model
call that can run for minutes:

  - **Opt-in.** Off unless LANGSMITH_TRACING is truthy *and* LANGSMITH_API_KEY
    is set. An absent key is "off", not an error: a missing observability
    backend must never stop someone authoring a walkthrough.
  - **Non-blocking.** Requests are handed to a single background daemon
    thread. One worker, one FIFO queue — that ordering is what guarantees a
    run's PATCH cannot overtake its POST.
  - **Silent on failure.** Nothing in here raises into the caller, and the
    first transport failure prints one line and then goes quiet rather than
    spamming a long authoring run.

Environment:

    LANGSMITH_TRACING=true                          # required to enable
    LANGSMITH_API_KEY=lsv2_...                      # required to enable
    LANGSMITH_ENDPOINT=https://api.smith.langchain.com
    LANGSMITH_PROJECT="Captcher"
    CAPTCHER_LANGSMITH_MAX_CHARS=20000         # 0 disables truncation

**On that last one.** Prompts here are captured customer pages — the authoring
pass alone can carry close to a million tokens of third-party HTML. Shipping
all of it to a third-party service on every pass is both a bandwidth problem
and a disclosure one, so every string in a traced payload is clipped to
CAPTCHER_LANGSMITH_MAX_CHARS with a marker saying how much was dropped.
Raise it deliberately, knowing what leaves the machine.
"""
import atexit
import json
import os
import queue
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DEFAULT_ENDPOINT = "https://api.smith.langchain.com"
DEFAULT_PROJECT = "default"
# Per-string cap on traced payloads. 20K characters is roughly 10K tokens —
# enough to read the shape of a prompt and all of a reply, far short of a
# capture.
DEFAULT_MAX_CHARS = 20_000
# Short by design: a trace write must never become the slowest thing in an
# authoring run, and a dropped span is a better outcome than a stalled worker.
TIMEOUT = 10
# Bounded so a wedged endpoint costs memory instead of unbounded growth. Spans
# are dropped once it fills.
QUEUE_MAX = 512
# How long atexit waits for the queue to drain. `python3 author.py <id>` exits
# the moment the pass returns, so without this the run is posted and the
# process dies before the PATCH that closes it.
FLUSH_TIMEOUT = 10.0


# ------------------------------------------------------------------- config

class _Config:
    __slots__ = ("endpoint", "api_key", "project", "max_chars")

    def __init__(self, endpoint, api_key, project, max_chars):
        self.endpoint = endpoint
        self.api_key = api_key
        self.project = project
        self.max_chars = max_chars


_UNSET = object()
_config_cache = _UNSET
_config_lock = threading.Lock()


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except ValueError:
        return default


def _build_config():
    """Read the environment once. Returns a _Config, or None when tracing is off.

    Read lazily rather than at import: author.load_env() populates os.environ
    from .env during *its* import, and this module is imported from there, so
    reading at import time would see the pre-.env environment.
    """
    # LANGCHAIN_* are the pre-rename names for both vars and still what a lot
    # of existing setups export; honoured after the current ones.
    tracing = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    if not _truthy(tracing):
        return None
    api_key = ((os.environ.get("LANGSMITH_API_KEY") or "").strip()
               or (os.environ.get("LANGCHAIN_API_KEY") or "").strip())
    if not api_key:
        # Deliberately quiet. Someone who exported LANGSMITH_TRACING in a
        # shell where the key is not set wants their walkthrough authored, not
        # a warning on every call.
        return None
    endpoint = ((os.environ.get("LANGSMITH_ENDPOINT") or "").strip()
                or (os.environ.get("LANGCHAIN_ENDPOINT") or "").strip()
                or DEFAULT_ENDPOINT)
    project = ((os.environ.get("LANGSMITH_PROJECT") or "").strip()
               or (os.environ.get("LANGCHAIN_PROJECT") or "").strip()
               or DEFAULT_PROJECT)
    return _Config(endpoint.rstrip("/"), api_key, project,
                   _int_env("CAPTCHER_LANGSMITH_MAX_CHARS",
                            DEFAULT_MAX_CHARS))


def _config():
    global _config_cache
    if _config_cache is _UNSET:
        with _config_lock:
            if _config_cache is _UNSET:
                _config_cache = _build_config()
    return _config_cache


def reset():
    """Drop the cached config so the next span re-reads the environment.
    For tests; nothing in the app changes these vars at runtime."""
    global _config_cache
    with _config_lock:
        _config_cache = _UNSET


def enabled() -> bool:
    return _config() is not None


# ------------------------------------------------------------------ transport

_queue = None
_worker = None
_worker_lock = threading.Lock()
_idle = threading.Event()
_idle.set()
_warned = False


def _warn(message: str):
    """One line, once. A long authoring pass emits many spans and a broken
    endpoint would otherwise bury the output that matters."""
    global _warned
    if not _warned:
        _warned = True
        print(f"[langsmith] tracing disabled for the rest of this process: {message}")


def _send(cfg, method: str, path: str, body: dict):
    data = json.dumps(body, default=str).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.endpoint}{path}",
        data=data,
        headers={"x-api-key": cfg.api_key, "content-type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def _run_worker():
    while True:
        item = _queue.get()
        try:
            if item is None:
                return
            cfg, method, path, body = item
            try:
                _send(cfg, method, path, body)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                _warn(f"{method} {path} -> HTTP {e.code}: {detail}")
            except Exception as e:  # transport, DNS, TLS, anything
                _warn(f"{method} {path} -> {type(e).__name__}: {e}")
        finally:
            _queue.task_done()
            if _queue.empty():
                _idle.set()


def _ensure_worker():
    global _queue, _worker
    if _worker is not None:
        return
    with _worker_lock:
        if _worker is not None:
            return
        _queue = queue.Queue(maxsize=QUEUE_MAX)
        _worker = threading.Thread(target=_run_worker, name="langsmith-trace", daemon=True)
        _worker.start()
        atexit.register(flush)


def _enqueue(cfg, method: str, path: str, body: dict):
    _ensure_worker()
    _idle.clear()
    try:
        _queue.put_nowait((cfg, method, path, body))
    except queue.Full:
        _warn("trace queue is full — dropping spans")
        _idle.set()


def flush(timeout: float = FLUSH_TIMEOUT) -> bool:
    """Wait for queued spans to drain. Returns True if the queue emptied.

    Registered with atexit, and safe to call directly. Never raises — a trace
    that did not make it out is not worth a non-zero exit status.
    """
    if _worker is None:
        return True
    return _idle.wait(timeout)


# ---------------------------------------------------------------- payloads

_TRUNCATION_NOTE = "\n…[{n:,} more characters not sent to LangSmith]"


def _clip(value, limit: int):
    """Recursively cap every string in a payload. See the note on
    CAPTCHER_LANGSMITH_MAX_CHARS in the module docstring — the prompts
    here contain captured third-party pages."""
    if limit <= 0:
        return value
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + _TRUNCATION_NOTE.format(n=len(value) - limit)
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in value]
    return value


def _now():
    return datetime.now(timezone.utc)


def _segment(start: datetime, run_id: str) -> str:
    """One `dotted_order` segment: a sortable start stamp followed by the run
    id. Matches what the LangSmith SDK emits, including the microseconds —
    the ordering of sibling runs in the UI depends on the format being exact."""
    return start.strftime("%Y%m%dT%H%M%S%fZ") + run_id


# ---------------------------------------------------------------- the spans

class _NullSpan:
    """What every helper returns when tracing is off, so call sites need no
    `if enabled()` guard around their bookkeeping."""

    __slots__ = ()

    def set_outputs(self, outputs):
        pass

    def add_metadata(self, metadata):
        pass

    def set_error(self, error):
        pass


_NULL = _NullSpan()


class _Span:
    __slots__ = ("_cfg", "_id", "_trace_id", "_dotted_order", "_parent_id",
                 "_start", "_outputs", "_metadata", "_error", "_closed")

    def __init__(self, cfg, name, run_type, inputs, metadata, tags, parent):
        self._cfg = cfg
        self._id = str(uuid.uuid4())
        self._start = _now()
        self._outputs = None
        self._metadata = dict(metadata or {})
        self._error = None
        self._closed = False

        segment = _segment(self._start, self._id)
        if parent is not None:
            self._parent_id = parent._id
            self._trace_id = parent._trace_id
            self._dotted_order = parent._dotted_order + "." + segment
        else:
            self._parent_id = None
            self._trace_id = self._id
            self._dotted_order = segment

        body = {
            "id": self._id,
            "trace_id": self._trace_id,
            "dotted_order": self._dotted_order,
            "name": name,
            "run_type": run_type,
            "inputs": _clip(inputs or {}, cfg.max_chars),
            "start_time": self._start.isoformat(),
            "session_name": cfg.project,
            "extra": {"metadata": self._metadata},
        }
        if self._parent_id:
            body["parent_run_id"] = self._parent_id
        if tags:
            body["tags"] = list(tags)
        # Posted on entry rather than on exit so an authoring pass — which can
        # run for minutes — shows up in LangSmith while it is still running.
        _enqueue(cfg, "POST", "/runs", body)

    def set_outputs(self, outputs):
        self._outputs = outputs

    def add_metadata(self, metadata):
        self._metadata.update(metadata or {})

    def set_error(self, error):
        self._error = str(error)

    def _close(self):
        if self._closed:
            return
        self._closed = True
        # No trace_id / dotted_order here. They are creation-time properties,
        # and echoing them back makes the update endpoint re-validate the run's
        # position in the tree without the parent_run_id it was created with —
        # which it reads as a root run and rejects with "dotted_order must
        # contain a single part for root runs".
        body = {
            "end_time": _now().isoformat(),
            "extra": {"metadata": self._metadata},
        }
        if self._outputs is not None:
            body["outputs"] = _clip(self._outputs, self._cfg.max_chars)
        if self._error:
            body["error"] = _clip(self._error, self._cfg.max_chars)
        _enqueue(self._cfg, "PATCH", f"/runs/{self._id}", body)


# The server is a ThreadingHTTPServer, so two authoring passes can be in
# flight at once. Parentage has to be per-thread or their spans interleave.
_local = threading.local()


def _stack():
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    return stack


@contextmanager
def span(name: str, run_type: str = "chain", inputs=None, metadata=None, tags=None):
    """Trace a block as one LangSmith run, nested under any enclosing span.

    Yields something with set_outputs/add_metadata/set_error whether or not
    tracing is on. An exception propagates unchanged, with its type and
    message recorded on the run first.
    """
    cfg = _config()
    if cfg is None:
        yield _NULL
        return

    stack = _stack()
    parent = stack[-1] if stack else None
    try:
        sp = _Span(cfg, name, run_type, inputs, metadata, tags, parent)
    except Exception as e:  # never let bookkeeping break the caller
        _warn(f"could not start span {name!r}: {type(e).__name__}: {e}")
        yield _NULL
        return

    stack.append(sp)
    try:
        yield sp
    except BaseException as e:
        sp.set_error(f"{type(e).__name__}: {e}")
        raise
    finally:
        # Pop by identity: a nested span that somehow escaped its own finally
        # must not leave this one popping the wrong entry.
        if sp in stack:
            stack.remove(sp)
        try:
            sp._close()
        except Exception as e:
            _warn(f"could not close span {name!r}: {type(e).__name__}: {e}")


def chat_inputs(system: str, user: str) -> dict:
    """The `messages` shape LangSmith renders as a chat trace for an `llm` run."""
    return {
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user", "content": [{"type": "text", "text": user}]},
        ]
    }


def chat_outputs(text: str, usage: dict = None) -> dict:
    """The assistant reply, plus `usage_metadata` in the keys LangSmith reads
    for token counts and cost.

    Anthropic reports uncached input, cache reads and cache writes as three
    separate counts; LangSmith wants `input_tokens` to be all of the input,
    with the cached share broken out in `input_token_details`, which is what
    it prices at the cache rates. Passing only the uncached count — as this
    once did — understated every cost by the cached share, and the authoring
    passes cache most of their prompt. Total is derived because LangSmith
    shows it and will not add it up itself."""
    outputs = {"messages": [{"role": "assistant",
                             "content": [{"type": "text", "text": text}]}]}
    if usage:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            cache_read = usage.get("cache_read_input_tokens") or 0
            cache_write = usage.get("cache_creation_input_tokens") or 0
            total_input = input_tokens + cache_read + cache_write
            outputs["usage_metadata"] = {
                "input_tokens": total_input,
                "output_tokens": output_tokens,
                "total_tokens": total_input + output_tokens,
            }
            if cache_read or cache_write:
                outputs["usage_metadata"]["input_token_details"] = {
                    "cache_read": cache_read,
                    "cache_creation": cache_write,
                }
    return outputs
