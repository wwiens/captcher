"""Job registry for the long-running AI passes.

An authoring pass is an eight-iteration tool loop against Opus carrying up to
eight inlined screenshots. It can run for minutes. Before this module the
server tracked those runs as a set of strings (`AUTHORING`), which answered
exactly one question — "is something running for this id?" — and answered it
inconsistently, because the keys were sometimes the bare walkthrough id and
sometimes a `kind:id` composite. Three things were unreachable as a result:

  * **progress** — the pass wrote to stdout and the browser got a spinner;
  * **cancellation** — nothing held a handle on the run, so a mistyped edit
    instruction had to be paid for in full;
  * **honest exclusion** — two call sites checked membership under the lock
    and then released it *without* adding a key, so a save or a delete could
    interleave with the pass it had just checked for.

A Job is the missing handle. It owns its lifecycle (running → done/error/
cancelled), a bounded event log, a cancel flag and a deadline. Exclusion is
keyed on the walkthrough id alone, not on (id, kind): an authoring pass and a
render check on the same walkthrough are not safely concurrent — the pass
reads the sidecar the check writes — and neither is a save against either of
them. One active job per walkthrough, whatever its kind, is the rule that
makes that true by construction rather than by remembering to compose keys.

Cancellation is cooperative, which is the only kind available here: the work
is a blocking HTTP call inside a library we do not want to reach into. The
passes take a `progress` callable and call it at their natural boundaries
(per loop iteration, per page); `Job.progress` raises `Cancelled` from inside
that call when the flag is set or the deadline has passed. So a cancel takes
effect at the next boundary, not instantly — a model request already in
flight still completes and is still paid for. That is worth saying plainly in
the UI, and `cancel()` returns the state that lets it be said.

Nothing here imports the passes; the passes do not import this. The only
contract between them is the `progress(phase, message)` callable, which is
optional everywhere and defaults to a no-op, so a pass called from the CLI
behaves exactly as it did before.
"""

import threading
import time
import uuid
from collections import deque

# How long a finished job stays readable. A client that loses its event
# stream mid-pass (laptop lid, flaky wifi) needs the result to still be there
# when it reconnects, and the catalog polls on a 10s cycle. Ten minutes is
# far past both and still bounds the registry on a long-running server.
RETENTION_SECONDS = 600

# Default wall-clock ceiling for one pass. The agentic loop is capped at 8
# iterations, each of which can be a slow Opus call with images; 20 minutes
# is comfortably above the worst run observed and well below "wedged
# forever". Overridable per claim so a cheap pass can hold a tighter one.
DEFAULT_TIMEOUT_SECONDS = 20 * 60

# Events kept per job. A pass emits on the order of tens of these; the cap is
# a runaway guard, not a budget. Oldest are dropped first, and `seq` keeps
# increasing so a reader can tell it missed some.
MAX_EVENTS = 500

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

TERMINAL_STATES = (DONE, ERROR, CANCELLED)


class Cancelled(Exception):
    """Raised inside a pass, from its own `progress` callback, to unwind it.

    Deliberately not an `AuthorError`: that type is the "show this to the
    author" channel and the handlers turn it into a 422. A cancellation is
    neither an error nor the author's problem to read about.
    """


class JobBusy(Exception):
    """A job already holds this walkthrough. Carries the holder so the caller
    can say *what* is running rather than just that something is."""

    def __init__(self, job):
        super().__init__(f"{job.kind} is already running for {job.walkthrough_id}")
        self.job = job


class Job:
    def __init__(self, walkthrough_id, kind, owner=None,
                 timeout=DEFAULT_TIMEOUT_SECONDS):
        self.id = uuid.uuid4().hex[:16]
        self.walkthrough_id = walkthrough_id
        self.kind = kind
        self.owner = owner
        self.state = RUNNING
        self.result = None
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self.deadline = self.started_at + timeout if timeout else None
        self._cancel = threading.Event()
        self._events = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        # One condition guards state, result, error and the event log, and is
        # what a streaming reader blocks on. Notify-all on every mutation:
        # there may be several readers (two tabs open on the catalog) and
        # they are cheap to wake.
        self._cond = threading.Condition()
        self.emit(kind, "started")

    # ---- progress -------------------------------------------------------

    def emit(self, phase, message=""):
        """Record a progress event and wake anyone streaming this job."""
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "at": time.time(),
                                 "phase": phase, "message": str(message)})
            self._cond.notify_all()

    def progress(self, phase, message=""):
        """The callable handed to a pass. Emits, and is also the point where
        a cancel or a timeout takes effect — the pass calls this at its own
        boundaries, so unwinding from here never leaves a half-written file
        the way killing a thread would."""
        self.raise_if_finished()
        self.emit(phase, message)

    def raise_if_finished(self):
        if self._cancel.is_set():
            raise Cancelled("cancelled by the author")
        if self.deadline and time.time() > self.deadline:
            raise Cancelled(f"timed out after {int(time.time() - self.started_at)}s")

    # ---- lifecycle ------------------------------------------------------

    def cancel(self):
        """Request cancellation. Returns False if the job had already
        finished — the caller has nothing to stop and should say so."""
        with self._cond:
            if self.state != RUNNING:
                return False
        self._cancel.set()
        self.emit("cancelling", "waiting for the current step to finish")
        return True

    @property
    def cancel_requested(self):
        return self._cancel.is_set()

    def _settle(self, state, result=None, error=None):
        with self._cond:
            # A pass that raced past its own cancellation check and finished
            # cleanly is reported as finished: the work exists on disk, and
            # calling it "cancelled" would tell the author their walkthrough
            # was untouched when it was not.
            self.state = state
            self.result = result
            self.error = error
            self.finished_at = time.time()
            self._cond.notify_all()

    def finish(self, result=None):
        self._settle(DONE, result=result)
        self.emit(DONE, "finished")

    def fail(self, error):
        self._settle(ERROR, error=str(error))
        self.emit(ERROR, str(error))

    def cancelled(self, reason="cancelled"):
        self._settle(CANCELLED, error=str(reason))
        self.emit(CANCELLED, str(reason))

    @property
    def finished(self):
        return self.state in TERMINAL_STATES

    # ---- reading --------------------------------------------------------

    def snapshot(self, include_events=True):
        with self._cond:
            out = {
                "job_id": self.id,
                "kind": self.kind,
                "walkthrough_id": self.walkthrough_id,
                "state": self.state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
                "cancel_requested": self._cancel.is_set(),
            }
            if self.result is not None:
                out["result"] = self.result
            if self.error is not None:
                out["error"] = self.error
            if include_events:
                out["events"] = list(self._events)
                out["seq"] = self._seq
            return out

    def events_since(self, seq, timeout=25.0):
        """Block until there is an event newer than `seq`, then return
        (events, state). Returns an empty list on timeout so the caller can
        send a keep-alive and come back — an idle model call can easily
        outlast any proxy's read timeout, and a stream that says nothing for
        four minutes gets closed underneath us.
        """
        deadline = time.time() + timeout
        with self._cond:
            while True:
                fresh = [e for e in self._events if e["seq"] > seq]
                if fresh or self.state != RUNNING:
                    return fresh, self.state
                remaining = deadline - time.time()
                if remaining <= 0:
                    return [], self.state
                self._cond.wait(remaining)


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_id = {}
        self._active = {}   # walkthrough_id -> Job

    def claim(self, walkthrough_id, kind, owner=None,
              timeout=DEFAULT_TIMEOUT_SECONDS):
        """Take the walkthrough for `kind`, or raise JobBusy.

        The claim and the registration happen under one lock, so unlike the
        old check-then-release there is no window in which a second caller
        sees the walkthrough free.
        """
        with self._lock:
            self._sweep_locked()
            holder = self._active.get(walkthrough_id)
            if holder is not None and not holder.finished:
                raise JobBusy(holder)
            job = Job(walkthrough_id, kind, owner=owner, timeout=timeout)
            self._active[walkthrough_id] = job
            self._by_id[job.id] = job
            return job

    def release(self, job):
        """Drop the active claim. Safe to call twice; the job stays readable
        by id until it is swept, which is what lets a client come back for
        the result after the stream has closed."""
        with self._lock:
            if self._active.get(job.walkthrough_id) is job:
                del self._active[job.walkthrough_id]

    def get(self, job_id):
        with self._lock:
            return self._by_id.get(job_id)

    def active_for(self, walkthrough_id):
        with self._lock:
            job = self._active.get(walkthrough_id)
            return job if job is not None and not job.finished else None

    def active(self):
        with self._lock:
            return [j for j in self._active.values() if not j.finished]

    def _sweep_locked(self):
        cutoff = time.time() - RETENTION_SECONDS
        stale = [jid for jid, j in self._by_id.items()
                 if j.finished and (j.finished_at or 0) < cutoff]
        for jid in stale:
            job = self._by_id.pop(jid)
            if self._active.get(job.walkthrough_id) is job:
                del self._active[job.walkthrough_id]


# One registry per process. The server is threaded, not forked, so this is
# genuinely shared; a multi-process deployment would need this in shared
# state, exactly like the rate limiter (see TODO.md).
REGISTRY = Registry()


def claim(walkthrough_id, kind, owner=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    return REGISTRY.claim(walkthrough_id, kind, owner=owner, timeout=timeout)


def release(job):
    return REGISTRY.release(job)


def get(job_id):
    return REGISTRY.get(job_id)


def active_for(walkthrough_id):
    return REGISTRY.active_for(walkthrough_id)


def active():
    return REGISTRY.active()
