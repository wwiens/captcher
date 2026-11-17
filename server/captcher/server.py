#!/usr/bin/env python3
"""
Walkthrough Player server.

Serves:
  /                          walkthrough catalog
  /player/<walkthrough-id>        the walkthrough player (popup instructions;
                                  ?edit=1 serves the classic player's editor)
  /player1/<walkthrough-id>       the classic side-panel player (backup)
  /api/walkthroughs               catalog JSON
  /api/walkthroughs/<walkthrough-id>   a walkthrough definition
  /api/walkthroughs/<walkthrough-id>/export  standalone single-file HTML download
  /api/walkthroughs/<walkthrough-id>/export-zip    folder export, zipped
  /api/walkthroughs/<walkthrough-id>/export-scorm  SCORM content package, zipped (2004 or 1.2)
  /api/walkthroughs/<walkthrough-id>/embed   embed settings (GET) / save (POST)
  /api/walkthroughs/<walkthrough-id>/thumb   catalog card image (see thumbs.py)
  /embed/<walkthrough-id>    PUBLIC: the exported walkthrough served inline for
                             iframing on customer sites (per-walkthrough opt-in)
  /capture/<walkthrough-id>/<path>  captured page objects out of the captures
                               bucket (HTML gets the bridge script injected so
                               the player can drive it)
  /static/<path>             player assets

WHAT THE DATA MOVE CHANGED IN HERE

Two things, everywhere.

**A walkthrough is a row, not a (definition, capture folder) pair.** Every
route that used to unpack `(d, capture_root)` now holds a `store` row and reads
`row["definition"]`; the capture is addressed by the walkthrough's id through
`store.capture_*`, and nothing in this file composes a path or a capture key.

**Identity is a member-id uuid, not an email address.** `current_user()`,
`bearer_user()` and `request_user()` return `members.id`; the address is a
display attribute read off the member row for the pages and API responses that
show one. That is why route ids are no longer run through `ingest.slugify()`:
ids are opaque uuids, and slugifying one mangles it into a 404. `slugify`
survives only where a *title* becomes a slug.

Run:  python3 server.py [port]     (default port 8080)
      CAPTCHER_HOST sets the bind address (default 127.0.0.1; use 0.0.0.0
      to accept connections from other machines on the network)
"""
import json
import os
import re
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from html import escape

from . import auth
from . import author
from . import blobs
from . import brandfetch
from . import branding
from . import db
from . import diagnostics
from . import store
from . import thumbs
from . import export
from . import ingest
from . import jobs
from . import library
from . import paths
from . import render_check
from . import scorm
from . import templates
from . import usage
from . import verify_pass
from http.cookies import SimpleCookie

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


def _env(name, default=""):
    """A configuration knob, with the empty string treated as unset.

    A name exported as "" is how a shell says "not configured", and every knob
    below wants that to mean the default rather than a configured emptiness —
    an empty EXAMPLE_WALKTHROUGHS would otherwise be a one-element list of "".
    The
    value is stripped too: these are hand-edited in .env, and a trailing space
    on a URL or a flag is a typo, never an intention.
    """
    return (os.environ.get(name) or "").strip() or default


MAX_UPLOAD = 200 * 1024 * 1024  # capture ZIPs carry a full page snapshot per step

# Cap for every other request body (forms, JSON, walkthrough definitions).
# A walkthrough definition with many steps is the largest of these by far and
# is still orders of magnitude under this.
MAX_BODY = 8 * 1024 * 1024

# Long-running AI work is tracked by jobs.py, which owns exclusion, progress,
# cancellation and the deadline. The old `AUTHORING` set lived here and is
# gone: it could only answer "is something running for this id?", it answered
# it under two different key shapes, and two call sites checked it and then
# released the lock without claiming anything (see handle_put_walkthrough and
# handle_delete, both of which now hold a job for the duration of the write).

# How each job kind is named in a message the author reads. The kind itself is
# the wire value (stable, matched by the front ends); this is the prose.
# The editor save and the delete are bounded local file work, not model calls.
# They take a job only for exclusion, so their deadline is short enough that a
# wedged one cannot lock a walkthrough out for the AI passes' 20 minutes.
SHORT_JOB_TIMEOUT = 120

JOB_LABELS = {
    "author": "An authoring pass",
    "ai-edit": "An AI edit",
    "render-check": "A render check",
    "verify-replay": "A replay check",
    "save": "A save",
    "delete": "A delete",
    "restore": "A restore",
    "reset": "A reset to draft",
}

# What may appear in a download's filename. Anything else becomes "_".
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# The shape a capture's own `createdAt` must have to reach the `recorded_at`
# column. It is written by the extension, so it is client-supplied — and it is
# now going into a timestamptz rather than sitting inertly inside a JSON file,
# where an unparseable value would fail the INSERT and lose an upload that is
# otherwise perfectly good. A capture whose timestamp does not match simply has
# none, and store.captured_at falls back to when the row was created.
ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")

# history stamps as written by store.save_with_history (%Y%m%d-%H%M%S). The
# restore endpoint matches against this before the stamp goes anywhere near a
# path, so a traversal attempt fails as a plain 400.
STAMP_RE = re.compile(r"^\d{8}-\d{6}$")

# Developer mode (`captcher --dev`, or CAPTCHER_DEBUG=1): restart when the .py
# files change, re-read templates on every request, and print full tracebacks
# for unexpected errors. Off by default: it is for working on Captcher, not
# for using it.
DEBUG = _env("CAPTCHER_DEBUG", "0").lower() not in ("", "0", "false", "no")

# In debug mode templates are re-read when their mtime changes, so editing a
# file under templates/ shows up on refresh. In production they are read once.
templates.set_debug(DEBUG)

# ---- credential-route throttling ------------------------------------------
# Per client, so somebody guessing the access token at /unlock gets a bounded
# number of tries and cannot spend anyone else's budget.
#
# Deliberately not a general-purpose limiter. It guards exactly the routes that
# accept credentials from an anonymous caller.
UNLOCK_RATE_LIMIT = int(_env("CAPTCHER_UNLOCK_RATE_LIMIT") or 20)
UNLOCK_RATE_WINDOW = int(_env("CAPTCHER_UNLOCK_RATE_WINDOW") or 900)

# X-Forwarded-For is trivially forged, so it is ignored unless the deployment
# says it sits behind a proxy that sets it. Trusting it by default would let an
# attacker mint a fresh identity per request and make the limiter decorative.
TRUST_PROXY = _env("CAPTCHER_TRUST_PROXY").lower() in (
    "1", "true", "yes", "on")


def _tristate_env(name):
    """None when unset — which for CAPTCHER_SECURE_COOKIES means "decide
    per request from the proxy header" rather than "off"."""
    raw = _env(name).lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on")


# Force the session cookie's Secure flag on or off. Unset means: on only when a
# trusted proxy says the request arrived over HTTPS. Forcing it on while serving
# plain http signs everybody out — the browser accepts the cookie and then
# never sends it back.
SECURE_COOKIES = _tristate_env("CAPTCHER_SECURE_COOKIES")


class RateLimiter:
    """A fixed-window counter per key. Small, in-memory, and good enough.

    Not shared across processes and dropped on restart — both acceptable for a
    brute-force speed bump, and neither worth a dependency.
    """

    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        """Record an attempt; False once the key is over its budget."""
        now = time.time()
        with self._lock:
            if len(self._hits) > 4096:  # never grow without bound
                self._hits = {k: v for k, v in self._hits.items()
                              if v[0] + self.window > now}
            started, count = self._hits.get(key, (now, 0))
            if started + self.window <= now:
                started, count = now, 0
            count += 1
            self._hits[key] = (started, count)
            return count <= self.limit

    def reset(self):
        with self._lock:
            self._hits.clear()


UNLOCK_LIMITER = RateLimiter(UNLOCK_RATE_LIMIT, UNLOCK_RATE_WINDOW)

# The capture extension authenticates with a per-member API token sent as
# "Authorization: Bearer <token>". Each member generates their token on the
# /account page (or `captcher token`); the server resolves the
# token to that member and stamps their uploads accordingly. There is
# deliberately no server-wide token in the environment: one secret shared by
# every installation cannot be revoked for a single member, and an upload
# carrying it could not be attributed to anyone.

WATCHED_FILES = [ROOT / "server.py", ROOT / "author.py", ROOT / "ingest.py",
                 ROOT / "export.py", ROOT / "auth.py", ROOT / "templates.py",
                 ROOT / "langsmith_trace.py", ROOT / "library.py"]
# Files under templates/ are deliberately NOT watched: templates.py re-reads
# them by mtime in debug mode, so editing markup needs no restart at all.

# Paths reachable before the server is unlocked (only relevant with
# CAPTCHER_ACCESS_TOKEN set; on loopback every browser request is the owner).
# Everything else redirects to /unlock. /static/ holds only player chrome
# (css/js/fonts/logo) — no user data — and the unlock page needs it;
# /favicon.ico is requested by the browser. /embed/ serves walkthroughs that
# were EXPLICITLY published for embedding on other sites (per-walkthrough
# opt-in, `store.is_embeddable`) — see do_GET.
PUBLIC_PREFIXES = ("/static/", "/embed/", "/examples/")
PUBLIC_PATHS = ("/favicon.ico", "/unlock")

# Hostnames a loopback-bound server answers to. Anything else in the Host header
# is refused, which is what stops a web page from reaching this server through
# DNS rebinding (its own hostname, re-pointed at 127.0.0.1). Add names with
# CAPTCHER_ALLOWED_HOSTS (comma-separated) when a proxy or a local alias needs
# them. With CAPTCHER_ACCESS_TOKEN set the check is off: every request then
# needs the access cookie, which a rebinding page cannot have.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTS = LOOPBACK_HOSTS | {
    h.strip().lower() for h in _env("CAPTCHER_ALLOWED_HOSTS").split(",") if h.strip()}

# Extension pairing (docs/pairing.md). Bumped only for a change the extension has to
# notice: /api/extension/ping advertises it so a panel newer than its server —
# or older — can say so instead of failing with a shapeless error. The two
# pairing routes are public in a narrow sense: one is guarded by a one-time code
# and the other answers a single yes/no.
PAIRING_API_VERSION = 1

# Two optional fields on the same ping, and the only way this server can say
# anything unplanned to an extension that is already installed.
#
# A Web Store extension cannot be updated on demand: a bad release is on a
# customer's machine until they take an update, and the review that gates it
# runs for days. So the server keeps a channel that needs no client release.
#
#   CAPTCHER_EXTENSION_NOTICE   free text the panel shows verbatim, in a
#                                    banner above every screen. For the things
#                                    a version number cannot express: "uploads
#                                    are paused until 14:00 UTC, your recording
#                                    is safe", "we move to a new address on the
#                                    14th". Keep it under 300 characters; the
#                                    panel truncates past that.
#   CAPTCHER_MIN_EXTENSION      the oldest extension version this server
#                                    still works with, e.g. "1.2.0". A panel
#                                    below it stops offering to pair and tells
#                                    the user to update instead of failing
#                                    somewhere further in.
#
# Both are unset by default, and an extension that predates them ignores them,
# so setting either is safe at any time.
EXTENSION_NOTICE = _env("CAPTCHER_EXTENSION_NOTICE")
MIN_EXTENSION_VERSION = _env("CAPTCHER_MIN_EXTENSION")

# The signed-out page can feature up to five real, published walkthroughs.
# Publication remains explicit: naming one here does not make a private
# walkthrough public. Each must also be published for embedding, exactly as it
# would be for a customer-site embed.
#
# These are SLUGS, not ids. The list is written by a person in configuration and
# a row of uuids would be unmaintainable — which is the whole reason the slug
# column outlived the slug-shaped primary key. `store.get_public_by_slug` owns
# the resolution, including what to do when two accounts publish the same slug.
EXAMPLE_WALKTHROUGH_SLUGS = tuple(
    slug.strip()
    for slug in _env("CAPTCHER_EXAMPLE_WALKTHROUGHS").split(",")
    if slug.strip()
)[:5]

def watch_and_reload(interval=1.0):
    """Restart the server process when a watched source file changes.
    (Walkthroughs, captures, and static files are re-read per request already,
    so only the Python files need a restart.)"""
    stamps = {}
    for f in WATCHED_FILES:
        try:
            stamps[f] = f.stat().st_mtime
        except OSError:
            pass

    def loop():
        pending = set()
        while True:
            time.sleep(interval)
            for f, m in stamps.items():
                try:
                    if f.stat().st_mtime != m:
                        pending.add(f.name)
                except OSError:
                    continue
            if not pending:
                continue
            # Never execv out from under a running pass. The old loop did,
            # and saving author.py mid-run killed an Opus call that had
            # already been paid for, with no cleanup and no message — the
            # browser just saw the connection drop. Editing a watched file
            # during a pass is exactly what someone iterating on the prompts
            # does, so this is not a rare race.
            running = jobs.active()
            if running:
                print(f"[debug] {', '.join(sorted(pending))} changed — waiting for "
                      f"{len(running)} running job(s) before restarting")
                continue
            print(f"[debug] {', '.join(sorted(pending))} changed — restarting server")
            # listening sockets are non-inheritable, so the fresh process can
            # rebind the port immediately. orig_argv, not argv: under
            # `python -m captcher` argv[0] is a path to __main__.py, which
            # cannot be run as a script. The browser tab is already open.
            os.environ["CAPTCHER_NO_BROWSER"] = "1"
            os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])

    threading.Thread(target=loop, daemon=True).start()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webm": "video/webm",
    ".ico": "image/x-icon",
    # Without this the woff2 subsets go out as application/octet-stream.
    # @font-face tolerates that (the browser sniffs), but a
    # <link rel="preload" as="font" type="font/woff2"> does not: a preload
    # whose response type does not match is dropped and the font is fetched a
    # second time, which is the opposite of the point.
    ".woff2": "font/woff2",
}

# Assets under /static/ whose bytes never change without the filename changing.
# These are the only responses in the server that are allowed to sit in a
# browser cache — see the Cache-Control note in send() for why everything else
# is no-store.
IMMUTABLE_STATIC_SUFFIXES = {".woff2", ".png", ".svg", ".ico", ".jpg"}

BRIDGE_TAG = b'<script src="/static/bridge.js"></script>'


def _json_value(value):
    """JSON for a value json.dumps cannot encode on its own.

    Only timestamps reach here in practice: `db` hands back
    `timestamptz` columns as datetimes, and several routes forward a row
    wholesale. UTC ISO-8601, the same shape the rest of the API already speaks.
    Anything else is stringified rather than raising, because a 500 on a
    serialisation detail is a worse answer than an approximate one.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def capture_html_bytes(body: bytes) -> bytes:
    """Make one captured page playable in the live player.

    Byte-for-byte the injection the /capture/ route has always done; only where
    the bytes come from changed (a Storage object rather than a file). It stays
    in the bytes domain rather than decoding: a captured page is multi-MB of
    frozen HTML in an encoding this server does not control, and a
    decode/re-encode round trip on every page load would cost real time and
    could not round-trip cleanly anyway.

    Three things happen, and all three are load-bearing:

      1. The frame sandbox is opened so the bridge can see into captured
         same-origin iframes — see export.unlock_frames for why the stamp match
         is byte-exact.
      2. `export.freeze_page_bytes` neuters the page's network primitives
         before its own scripts run, the same shim exports inject. Before this,
         the live route was the one place captured JS ran unfrozen, and a real
         capture's survey widget popped up in the player render only (the
         render check caught it; FIELD-NOTES.md 2026-07-30).
      3. bridge.js is tagged in at the end of the body, exactly where the
         export inlines it.
    """
    body = body.replace(export.FRAME_SANDBOX_STAMP.encode(),
                        export.FRAME_SANDBOX_OPEN.encode())
    body = export.freeze_page_bytes(body)
    if b"</body>" in body:
        return body.replace(b"</body>", BRIDGE_TAG + b"</body>", 1)
    return body + BRIDGE_TAG


# The download routes under /api/walkthroughs/<id>/… Longest suffix first, so
# "/export" can never shadow "/export-zip" or "/export-scorm".
EXPORT_SUFFIXES = ("/export-scorm", "/export-zip", "/export")


def _local_path_or_root(target):
    """A `next`-style redirect target reduced to a path on this server, or "/".

    The value comes off a URL, so it is honoured only when it is a plain
    absolute path. Three shapes are refused: anything not starting with "/",
    "//host" (a protocol-relative URL, not a path), and anything containing a
    backslash — browsers normalise "/\\evil.example" to "//evil.example", so a
    startswith("//") check alone is bypassable. Control characters would split
    the Location header and are refused for the same reason.
    """
    target = (target or "").strip()
    if (not target.startswith("/") or target.startswith("//")
            or "\\" in target or any(ord(c) < 0x20 or c == "\x7f" for c in target)):
        return "/"
    return target


def parse_multipart(body: bytes, boundary: str):
    """Minimal multipart/form-data parser (stdlib only).

    Returns {field_name: (filename_or_None, content_bytes)}.
    """
    fields = {}
    delim = b"--" + boundary.encode("utf-8")
    for part in body.split(delim):
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        disposition = ""
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition = line
        name = re.search(r'name="([^"]*)"', disposition)
        if not name:
            continue
        filename = re.search(r'filename="([^"]*)"', disposition)
        fields[name.group(1)] = (filename.group(1) if filename else None, content)
    return fields


# The picked-element context the AI editor sends with an edit instruction.
# Every one of these strings is pasted into a model prompt, so they are capped
# here — the editor is not the only thing that can POST to /ai-edit.
SELECTION_FIELDS = {"page": 300, "selector": 300, "text": 200,
                    "text_exact": 200, "tag": 20, "html": 800}


def clean_selection(raw):
    """Reduce the editor's selection payload to short strings, or None.

    Anything malformed drops the WHOLE selection rather than the offending
    field: a half-kept selection still reads to the model as a confident
    "the author means this element", pointing it at the wrong one.
    """
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, limit in SELECTION_FIELDS.items():
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value:
            out[key] = value[:limit]
    if not out.get("selector"):
        return None   # without it there is nothing to point at
    matches = raw.get("matches")
    if isinstance(matches, int) and not isinstance(matches, bool) and 0 <= matches < 10000:
        out["matches"] = matches
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "WalkthroughPlayer/0.1"

    # ---- helpers -------------------------------------------------------
    def send(self, status, body, content_type="text/html; charset=utf-8",
             extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # no-store is the default and stays the default: almost everything here
        # is either a member's own data or a page whose staleness would show. A
        # caller that supplies its own Cache-Control wins outright rather than
        # having a second header appended beside this one, because two
        # Cache-Control lines is an argument the browser resolves for you.
        # Only the catalog thumbnails take that exit — see the /thumb route.
        supplied = {k.lower() for k, _ in (extra_headers or [])}
        if "cache-control" not in supplied:
            self.send_header("Cache-Control", "no-store")
        # Clickjacking. Every response goes out through here (send_file and
        # send_json both call it), so this is the one place to say it.
        #
        # SAMEORIGIN, not DENY: the player frames /capture/<id>/ pages from this
        # same origin, and DENY would break it. SAMEORIGIN still refuses the
        # framing that matters — someone else's page framing /account or the
        # editor and stealing a click.
        #
        # A caller that sets its own framing header wins, which is what keeps
        # /embed/<id> public: it sends `frame-ancestors *` deliberately so
        # customer sites can iframe it.
        if not supplied & {"x-frame-options", "content-security-policy"}:
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        # Captured pages, uploads and exports mean this server hands out a lot
        # of bytes whose type it declares itself; nosniff stops a browser from
        # second-guessing that declaration (a text/plain 404 body, a .json
        # walkthrough) into something it would execute.
        if "x-content-type-options" not in supplied:
            self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_not_modified(self, extra_headers=None):
        """A 304 for a conditional GET.

        Written out here rather than through `send()` because a 304 carries no
        body, and `send()` would give it a `Content-Length: 0` — which is not
        the same statement. Content-Length on a 304 describes the entity the
        client already holds, so declaring it zero tells a strict cache the
        thing it cached is empty.

        The validators go back out with it (RFC 9110 §15.4.5): a cache that
        drops the freshness headers on a revalidation re-asks on the very next
        request, which is the round trip this exists to avoid.
        """
        self.send_response(304)
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def if_none_match(self, etag):
        """Does the client already hold this exact entity?

        `If-None-Match` is a comma-separated list and may be `*`. Weak
        comparison is the one the spec asks for on a GET, so a `W/` prefix on
        either side is stripped before comparing rather than treated as a
        mismatch — a proxy is free to weaken a validator in transit.
        """
        header = self.headers.get("If-None-Match")
        if not header:
            return False
        def strip(t):
            return t.strip()[2:] if t.strip().startswith("W/") else t.strip()
        offered = strip(etag)
        return any(t == "*" or strip(t) == offered for t in header.split(","))

    def redirect(self, location, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def send_json(self, obj, status=200):
        # `default=_json_value` because rows come straight off the database now
        # and Postgres timestamps arrive as datetime objects, which json.dumps
        # refuses. Converting here rather than at each call site means a column
        # added to a stored row cannot 500 a route that merely passes it
        # through — which is exactly what /api/collections would have done with
        # the created_at/updated_at it has always returned.
        self.send(status, json.dumps(obj, indent=2, default=_json_value),
                  MIME[".json"])

    def job_busy(self, busy):
        """409 for a walkthrough already claimed by another job.

        Naming the holder matters: "an AI edit is already running" and "a
        render check is already running" send the author to different places,
        and the single message the old set could produce told them apart only
        by which endpoint they happened to have called.
        """
        job = busy.job
        return self.send_json(
            {"error": f"{JOB_LABELS.get(job.kind, job.kind)} is already running "
                      f"for {job.walkthrough_id} — wait for it to finish or "
                      f"cancel it.",
             "job_id": job.id, "kind": job.kind}, 409)

    # ---- long-running AI work -------------------------------------------
    #
    # Every AI pass goes through run_as_job. Two shapes come out of it:
    #
    #   background=False — the historical contract. The pass runs in this
    #     request thread and the final JSON is the response. Every existing
    #     caller (the player's AI edit, the review page's two checks) keeps
    #     working untouched, and they gain cancellation for free, because the
    #     job is registered before the work starts and a *different* request
    #     can now reach it.
    #   background=True — 202 + {"job_id"}. The caller then watches
    #     /api/jobs/<id>/events and picks the result out of the terminal
    #     event. This is what the catalog uses, because an authoring pass is
    #     the one that can run for minutes behind a spinner.
    #
    # Opting in per request rather than flipping the endpoints wholesale is
    # deliberate: the response shape is a contract with three separate front
    # ends, and only one of them hurts today.
    def run_as_job(self, walkthrough_id, kind, work, *, background=False,
                   soft_errors=(), timeout=jobs.DEFAULT_TIMEOUT_SECONDS):
        """Claim `walkthrough_id` for `kind`, then run `work(job) -> dict`.

        `soft_errors` are the module's own "tell the author" exceptions and
        map to 422, matching what these handlers returned before. Anything
        else is a 500 with a traceback under DEBUG, as before.

        """
        member = self.request_user()
        try:
            job = jobs.claim(walkthrough_id, kind, owner=member,
                             timeout=timeout)
        except jobs.JobBusy as busy:
            return self.job_busy(busy)

        # Which status the synchronous path should return for a failure. The
        # background path does not need it — its client reads the job, and a
        # job that failed carries the message either way.
        outcome = {"status": 422}

        def run():
            try:
                # Set here, inside run(), so it is in force on whichever thread
                # does the work: every model call the job makes is attributed
                # to this kind, walkthrough, member and job (usage.py).
                with usage.attribute(kind, walkthrough_id=walkthrough_id,
                                     member_id=member, job_id=job.id):
                    result = work(job)
            except jobs.Cancelled as e:
                job.cancelled(str(e))
            except soft_errors as e:
                job.fail(str(e))
            except Exception as e:
                if DEBUG:
                    traceback.print_exc()
                outcome["status"] = 500
                job.fail(f"{JOB_LABELS.get(kind, kind)} failed unexpectedly: {e}")
            else:
                job.finish(result)
            finally:
                jobs.release(job)

        if background:
            threading.Thread(target=run, daemon=True,
                             name=f"{kind}:{walkthrough_id}").start()
            return self.send_json({"ok": True, "job_id": job.id, "kind": kind,
                                   "walkthrough_id": walkthrough_id}, 202)

        run()
        if job.state == jobs.DONE:
            return self.send_json({"ok": True, "job_id": job.id, **job.result})
        if job.state == jobs.CANCELLED:
            return self.send_json({"error": f"Cancelled: {job.error}",
                                   "cancelled": True, "job_id": job.id}, 409)
        return self.send_json({"error": job.error, "job_id": job.id},
                              outcome["status"])

    def send_event_stream(self, job, since=0):
        """Stream one job's progress as Server-Sent Events.

        ThreadingHTTPServer gives every connection its own thread, so holding
        one open for the length of a pass costs a thread and nothing else.
        There is no Content-Length and no buffering: each event is written and
        flushed as it happens.

        A keep-alive comment goes out whenever the job has been quiet for a
        while. A model call can think for minutes without emitting anything,
        and a stream that says nothing at all gets closed by whatever proxy
        sits in front of it — the comment is invisible to EventSource and
        keeps the connection legibly alive.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Named for nginx, harmless everywhere else: without it a buffering
        # proxy holds the events until the pass finishes, which is exactly
        # the spinner this endpoint exists to replace.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def write(chunk):
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        try:
            # Replay whatever the job has already recorded, so a client that
            # connects late (or reconnects) sees the whole run, not the tail.
            write(f"retry: 3000\n\nevent: state\ndata: "
                  f"{json.dumps(job.snapshot())}\n\n")
            while True:
                events, state = job.events_since(since)
                if not events and state == jobs.RUNNING:
                    write(": keep-alive\n\n")
                    continue
                for e in events:
                    since = e["seq"]
                    write(f"id: {e['seq']}\nevent: progress\ndata: "
                          f"{json.dumps(e)}\n\n")
                if state != jobs.RUNNING:
                    write(f"event: done\ndata: "
                          f"{json.dumps(job.snapshot(include_events=False))}\n\n")
                    return
        except (BrokenPipeError, ConnectionResetError):
            # The tab was closed. That is not a cancellation — an authoring
            # pass the author navigated away from should still land, and they
            # can pick the result up from the catalog when they come back.
            return

    def content_length(self):
        """Content-Length as an int; 0 when absent or unparseable."""
        try:
            return max(0, int(self.headers.get("Content-Length", "0")))
        except ValueError:
            return 0

    def _read_body(self, cap=MAX_BODY):
        """Read at most `cap` bytes of the body.

        Only /api/upload legitimately sends anything large, and it enforces
        MAX_UPLOAD itself. Everything else is a form post or a walkthrough
        definition, so a cap keeps an oversized Content-Length from making the
        server allocate arbitrarily — this is a ThreadingHTTPServer, one thread
        per connection.
        """
        n = self.content_length()
        return self.rfile.read(min(n, cap)) if n else b""

    def read_form(self):
        """Body parsed as x-www-form-urlencoded."""
        return parse_qs(self._read_body().decode("utf-8", "replace"))

    def read_json(self):
        """Body parsed as JSON: {} for an empty body, None if it does not
        parse. Callers that require a non-empty object must check for it."""
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None

    def branding_body(self, reject_mybrand=True):
        """Shared preamble for the branding mutation handlers.

        Returns (body, profile_id, owner), or None after having already sent
        the error response — callers do `if parsed is None: return`.

        Brand profiles are a browser-only flow, so this requires a login
        session and deliberately does NOT accept the extension's capture
        token, even though do_POST's authorized() gate does. Narrowing what a
        leaked capture token can reach costs nothing here, and it is what makes
        the owner below a real member rather than a maybe-None the branding
        queries would have to defend against.

        The id is taken verbatim. It used to be run through `ingest.slugify`,
        which was harmless while profile ids were slugs and is not now: a
        profile id is a uuid, and slugifying one leaves a string that matches
        no row. Everything downstream filters on owner_id in the same query, so
        a malformed id resolves to "no such profile" rather than to somebody
        else's.

        The body is read before the session check so that rejecting does not
        leave an unread body on the connection (see reject_unauthorized).
        """
        body = self.read_json()
        owner = self.current_user()
        if not owner:
            self.send_json(
                {"error": "Unlock this server at /unlock to manage brand profiles. A capture "
                          "token is for uploading captures only."}, 401)
            return None
        if not isinstance(body, dict):
            self.send_json({"error": "Expected a JSON body"}, 400)
            return None
        pid = str(body.get("id") or "").strip()
        if reject_mybrand and pid == branding.MYBRAND_ID:
            self.send_json({"error": "Your brand profile is read-only"}, 403)
            return None
        return body, pid, owner

    def download(self, data, content_type, filename):
        """Send `data` as a file attachment.

        The filename is reduced to a safe character set before it is placed in
        a header. Every caller builds it from `export.export_basename`, i.e.
        from the row's slug — which is a person-chosen title run through
        `store.slugify` and therefore already tame, but it is stored data
        rather than a derived constant, so it must not be able to put a quote
        or a CR/LF into a response header."""
        safe = _SAFE_FILENAME_RE.sub("_", filename).strip("._") or "download"
        return self.send(200, data, content_type,
                         extra_headers=[("Content-Disposition",
                                         f'attachment; filename="{safe}"')])

    def handle_export(self, rest, walkthroughs, owner):
        """The three export downloads: single-file HTML, folder ZIP, SCORM ZIP.

        They differ only in which builder runs, the media type, and the
        filename — everything else (id resolution, brand profile, intro flag,
        ExportError handling, attachment headers) was copy-pasted three times.

        The download is NAMED from the row's slug and ADDRESSED by its uuid.
        `export.export_basename` owns that split: a file called
        `3f2a1b4c-7e19-….html` sitting in someone's Downloads folder, or a
        SCORM package named after one in an LMS course list, is unidentifiable
        to the person who has to find it again.
        """
        suffix = next(s for s in EXPORT_SUFFIXES if rest.endswith(s))
        walkthrough_id = rest[:-len(suffix)]
        row = walkthroughs.get(walkthrough_id)
        if row is None:
            return self.no_such_walkthrough(walkthrough_id)
        stem = export.export_basename(row)

        query = parse_qs(urlparse(self.path).query)
        brand = distribution_brand(walkthrough_id, query.get("profile", [None])[0], owner)
        skip_intro = query.get("intro", ["1"])[0] == "0"
        try:
            if suffix == "/export":
                return self.download(
                    export.build_export(walkthrough_id, brand, skip_intro),
                    MIME[".html"], f"{stem}.html")
            if suffix == "/export-zip":
                return self.download(
                    export.build_export_zip(walkthrough_id, brand, skip_intro),
                    "application/zip", f"{stem}.zip")
            version = query.get("version", ["2004"])[0]
            return self.download(
                scorm.build_export_scorm_zip(walkthrough_id, brand, skip_intro,
                                             version=version),
                "application/zip", f"{stem}-scorm.zip")
        except export.ExportError as e:
            return self.send_json({"error": f"Export failed: {e}"}, 422)

    def no_such_walkthrough(self, walkthrough_id):
        """404 for an API route. Always names the id — several call sites used
        to return a bare message with no id, which said nothing useful."""
        return self.send_json({"error": f"Unknown walkthrough: {walkthrough_id}"}, 404)

    def no_such_walkthrough_page(self, walkthrough_id):
        """404 for a browser-facing route (player, review, capture assets),
        where a person reads the response and raw JSON would be noise."""
        return self.send(404, f"Unknown walkthrough: {walkthrough_id}", "text/plain")

    def send_file(self, path: Path, extra_headers=None):
        """Send one file off the local disk: /static/ and the favicon.

        These are application code shipped with the repo, not member data. The
        `inject_bridge` branch that used to live here is gone with the capture
        folder — captured pages are objects now and go out through
        `capture_html_bytes` on the /capture/ route, which is the only caller
        it ever had.
        """
        if not path.is_file():
            return self.send(404, "Not found", "text/plain")
        self.send(200, path.read_bytes(),
                  MIME.get(path.suffix.lower(), "application/octet-stream"),
                  extra_headers=extra_headers)

    def current_user(self):
        """The member behind this browser request, or None.

        A valid signed cookie names its member. Without one, a request is the
        local owner — unless CAPTCHER_ACCESS_TOKEN is set, in which case the
        cookie from /unlock is required and there is no default.

        A request carrying a Bearer token is the capture extension, never the
        browser, whatever else it carries: otherwise a capture token alone
        would pass every browser-only route (pairing, minting tokens, revoking
        devices) as the local owner, and a leaked token could mint its own
        replacements.
        """
        if self.headers.get("Authorization", "").startswith("Bearer "):
            return None
        raw = self.headers.get("Cookie", "")
        if raw:
            try:
                jar = SimpleCookie()
                jar.load(raw)
            except Exception:
                jar = {}
            morsel = jar.get(auth.SESSION_COOKIE)
            if morsel:
                member = auth.verify_session(morsel.value)
                if member:
                    return member
        if auth.ACCESS_TOKEN:
            return None
        return auth.ensure_local_owner()

    def bearer_user(self):
        """The member id owning the capture token in the Authorization header,
        or None. This is how the capture extension authenticates."""
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return auth.find_member_by_token(header[7:].strip())

    def request_user(self):
        """The member behind this request: the browser (see current_user), or
        the capture extension's per-device Bearer token."""
        return self.current_user() or self.bearer_user()

    def my_walkthrough(self, walkthrough_id):
        """The requester's walkthrough row, or None.

        One indexed lookup with the owner filter in the query, where this used
        to load every one of the member's walkthroughs to test membership of
        the resulting dict. Routes that need the row (most of them) take it
        from here instead of fetching again after the ownership check.

        Another member's walkthrough returns None exactly as a nonexistent one
        does, and both are the same 404 to the caller — deliberately, so the
        API cannot be used to probe for ids.
        """
        member_id = self.request_user()
        if member_id is None:
            return None
        return store.get(walkthrough_id, member_id)

    def owns_walkthrough(self, walkthrough_id):
        return self.my_walkthrough(walkthrough_id) is not None

    def wants_background(self):
        """`?background=1` opts this request into the job/202 shape.

        A query parameter rather than a body field so it reads the same on
        all four AI routes regardless of what their bodies look like, and so
        it is visible in a log line.
        """
        q = parse_qs(urlparse(self.path).query)
        return (q.get("background", [""])[0] or "").lower() in ("1", "true", "yes")

    def owns_job(self, job):
        """A job is readable and cancellable by the member who started it.

        Falls back to walkthrough ownership when the job predates a known
        owner (a job started by the capture extension's bearer token carries
        the same member, so this is belt-and-braces rather than a real gap).
        """
        user = self.request_user()
        if user is None:
            return False
        return job.owner == user or self.owns_walkthrough(job.walkthrough_id)

    def is_public(self, path):
        return (path in PUBLIC_PATHS
                or any(path.startswith(p) for p in PUBLIC_PREFIXES))

    def authorized(self):
        """Gate for mutating requests: the browser (see current_user) OR a valid
        per-device capture token from the extension."""
        return self.request_user() is not None

    def drain_body(self):
        """Read and discard the request body, a megabyte at a time. Refusing
        mid-upload resets the connection, and the client then sees a network
        error instead of the refusal."""
        try:
            remaining = min(int(self.headers.get("Content-Length", "0")), MAX_UPLOAD)
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)

    def reject_unauthorized(self):
        self.drain_body()
        self.send_json(
            {"error": "Unauthorized — unlock this server at /unlock, or (for the "
                      "capture extension) send an 'Authorization: Bearer <token>' "
                      "header with a capture token."}, 401)

    # ---- request safety: Host, cross-site writes, the access cookie ------
    def host_allowed(self):
        """Whether the Host header names this server (see ALLOWED_HOSTS).

        Always true in access-token mode, where the cookie is the protection.
        A request with no Host header at all (HTTP/1.0 tooling) is allowed: a
        browser always sends one, so it cannot be a rebinding page.
        """
        if auth.ACCESS_TOKEN:
            return True
        host = self.headers.get("Host", "").strip().lower()
        if not host:
            return True
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host \
            else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name in ALLOWED_HOSTS

    def cross_site_write(self, path):
        """True when a browser on another site sent this write request (CSRF).

        With no login, any page the owner visits could otherwise POST here.
        Requests carrying a Bearer token are exempt: a cross-site page cannot
        add that header without a CORS preflight this server never approves,
        and the token itself is checked afterwards. So is the extension's claim,
        whose one-time code is its credential. Non-browser clients send neither
        Sec-Fetch-Site nor Origin, and are not a CSRF vector.
        """
        if path == "/api/extension/claim":
            return False
        if self.headers.get("Authorization", "").startswith("Bearer "):
            return False
        site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        if site:
            return site not in ("same-origin", "none")
        origin = self.headers.get("Origin", "").strip()
        if origin:
            return origin != self.request_origin()
        return False

    def refuse_request(self, status, message):
        """Answer a refused request, discarding its body unread."""
        self.drain_body()
        return self.send(status, message, "text/plain")

    def secure_cookies(self):
        """Whether to mark the access cookie Secure.

        On a real deployment it must not travel in the clear. But this server
        is normally reached over plain http on 127.0.0.1, and an unconditional
        Secure flag would mean the browser accepts the Set-Cookie and never
        sends it back.

        So: on when the deployment says so, or when a *trusted* proxy reports
        the original request arrived over HTTPS. X-Forwarded-Proto is forgeable
        in exactly the way X-Forwarded-For is, and is read on the same terms.
        """
        if SECURE_COOKIES is not None:
            return SECURE_COOKIES
        if TRUST_PROXY:
            return self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"
        return False

    def cookie_flags(self):
        return "Path=/; HttpOnly; SameSite=Lax" + (
            "; Secure" if self.secure_cookies() else "")

    def set_cookie(self, token):
        return ("Set-Cookie",
                f"{auth.SESSION_COOKIE}={token}; {self.cookie_flags()}; "
                f"Max-Age={auth.SESSION_TTL}")

    def clear_cookie(self):
        # The flags must match the ones it was set with, or the browser treats
        # this as a different cookie and it survives.
        return ("Set-Cookie",
                f"{auth.SESSION_COOKIE}=; {self.cookie_flags()}; Max-Age=0")

    def client_ip(self):
        """The caller's address, honouring X-Forwarded-For only when the
        deployment says a proxy sets it (CAPTCHER_TRUST_PROXY)."""
        if TRUST_PROXY:
            # Railway's edge writes the client address to X-Real-IP and sets
            # no X-Forwarded-For at all; without this branch the fallback
            # below keyed the limiter on the socket address of whichever
            # edge node forwarded the request, which varies per request, and
            # twenty-one wrong passwords in a row were never throttled. A
            # single value the proxy owns outright is also the simpler
            # contract than counting back through an appended chain.
            real_ip = self.headers.get("X-Real-IP", "").strip()
            if real_ip:
                return real_ip
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                # The RIGHT-most entry, not the left-most. A proxy appends the
                # address it saw to whatever the client already sent, so the
                # left-most value is client-controlled: with it in use, one
                # attacker behind a trusted proxy could present a fresh
                # "client" per request and the limiter would be decorative
                # again — the very failure TRUST_PROXY exists to avoid. The
                # last entry is the one the trusted proxy itself wrote.
                # (Assumes one trusted hop; a deployment with a chain of
                # proxies would need to count back through the trusted ones.)
                return forwarded.rsplit(",", 1)[-1].strip() or "?"
        return self.client_address[0] if self.client_address else "?"

    def request_origin(self):
        """scheme://host as the browser addressed this server, for comparing
        with a request's Origin header. X-Forwarded-Host and -Proto are read
        only when CAPTCHER_TRUST_PROXY says a proxy sets them."""
        host = ""
        if TRUST_PROXY:
            host = self.headers.get("X-Forwarded-Host", "").strip()
        host = host or self.headers.get("Host", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
            return ""
        https = (TRUST_PROXY and self.headers.get(
            "X-Forwarded-Proto", "").strip().lower() == "https")
        return f"{'https' if https else 'http'}://{host}"

    def throttled(self, bucket):
        """True (and already answered) when this client has had enough goes."""
        if UNLOCK_LIMITER.allow(f"{bucket}:{self.client_ip()}"):
            return False
        return True

    def handle_unlock(self):
        """POST /unlock: trade CAPTCHER_ACCESS_TOKEN for the access cookie."""
        if not auth.ACCESS_TOKEN:
            self._read_body()
            return self.redirect("/")
        if self.throttled("unlock"):
            self._read_body()
            return self.send(429, unlock_page(
                error="Too many attempts. Wait a few minutes and try again."))
        token = self.read_form().get("token", [""])[0]
        if not auth.access_token_ok(token):
            return self.send(403, unlock_page(error="That access token is not right."))
        return self.redirect("/", extra_headers=[
            self.set_cookie(auth.make_session(auth.ensure_local_owner()))])

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if not self.host_allowed():
            return self.send(421, "This server does not answer to that host name.",
                             "text/plain")

        # Only meaningful with CAPTCHER_ACCESS_TOKEN set; otherwise there is
        # nothing to unlock and the catalog is one redirect away.
        if path == "/unlock":
            if not auth.ACCESS_TOKEN or self.current_user():
                return self.redirect("/")
            return self.send(200, unlock_page())
        # Token check for the capture extension ("Save & test"): identifies
        # the member behind the browser OR a capture-token Bearer header.
        if path == "/api/whoami":
            member = auth.get_member(self.request_user())
            if not member:
                return self.send_json(
                    {"error": "Invalid or missing capture token — connect the "
                              "extension again from a Captcher page."}, 401)
            # The extension shows the address, so the address is what goes on
            # the wire — read off the member row rather than being the identity
            # itself. The key stays `email`: it is a contract with a version of
            # the extension already installed in people's browsers.
            return self.send_json({"ok": True, "email": member["email"]})

        # "Is this a Captcher server?" — what drives the extension's whole
        # connect surface, so it is called on tab changes and not just on a
        # button press. Public by necessity (the extension has no credential
        # yet, which is the point being solved) and safe because it answers one
        # question the caller is about to learn anyway by posting here. No
        # member data, no account existence, nothing private.
        if path == "/api/extension/ping":
            # Additive: keys are included only when configured, so the body an
            # older extension sees is byte-for-byte the one it has always seen.
            answer = {"captcher": True, "pairing": PAIRING_API_VERSION,
                      # So a recorder can refuse a capture it has no chance of
                      # delivering rather than spending its whole upload
                      # deadline — up to ten minutes — earning a 413.
                      "maxUploadBytes": MAX_UPLOAD}
            if EXTENSION_NOTICE:
                answer["notice"] = EXTENSION_NOTICE
            if MIN_EXTENSION_VERSION:
                answer["minExtensionVersion"] = MIN_EXTENSION_VERSION
            return self.send_json(answer)

        # Everything below needs an owner. Without one (access-token mode, no
        # cookie yet), nothing reaches store.load_walkthroughs(), which without
        # an owner returns every member's records.
        if not self.is_public(path) and not self.current_user():
            return self.redirect("/unlock")

        # ---- full-size public example player. The configured-id and explicit
        # embed-publication checks live in example_page(), so an arbitrary or
        # private walkthrough id still returns the same plain 404. The legacy
        # /player suffix remains an alias for links opened before examples
        # became the full player page themselves.
        if path.startswith("/examples/"):
            remainder = path.split("/", 2)[2].strip("/")
            # A SLUG, kept verbatim. It is matched against the configured list
            # and then resolved by store.get_public_by_slug; running it through
            # slugify() would silently accept a mangled spelling of a real one.
            slug = remainder[:-len("/player")] if remainder.endswith("/player") else remainder
            try:
                page = example_page(slug)
            except export.ExportError as e:
                return self.send(500, f"Example build failed: {e}", "text/plain")
            if page is None:
                return self.send(404, "This walkthrough example is not published.",
                                 "text/plain")
            return self.send(200, page)

        # ---- public embed: the export served inline for customer-site iframes.
        # Only walkthroughs explicitly published for embedding are reachable;
        # everything else 404s without leaking whether the id exists. Brand +
        # intro were frozen at publish time.
        if path.startswith("/embed/"):
            # The id verbatim — it is a uuid off the URL, and store.get answers
            # None for anything that is not one. No owner filter: an embed is
            # deliberately cross-account public, which is the whole feature.
            walkthrough_id = path.split("/", 2)[2].strip("/")
            row = store.get(walkthrough_id)
            if row is None or not store.is_embeddable(row):
                return self.send(404, "This walkthrough is not published for embedding.",
                                 "text/plain")
            emb = store.load_embed_settings(row) or {}
            # The owner's brand profile, resolved per request so a branding
            # change shows on the live embed immediately.
            brand = distribution_brand(walkthrough_id, emb.get("profile"),
                                       row["owner_id"])
            chrome = emb.get("chrome") if emb.get("chrome") in ("full", "viewport") else "full"
            try:
                html = export.build_export(walkthrough_id, brand,
                                           skip_intro=not emb.get("intro", True),
                                           chrome=chrome)
            except export.ExportError as e:
                return self.send(500, f"Embed build failed: {e}", "text/plain")
            # explicitly allow any site to iframe this response
            return self.send(200, html,
                             extra_headers=[("Content-Security-Policy",
                                             "frame-ancestors *")])

        # ---- application assets, answered before any walkthrough is read.
        # They are the same bytes for everyone and involve no member data, and
        # the catalog read below is not free: it is a query returning every one
        # of this member's definitions. Left below it — where they used to sit —
        # a signed-out visitor's font and stylesheet requests each ran
        # `load_walkthroughs(None)`, which has no owner to filter by and so
        # pulls every walkthrough on the server. That was tolerable against a
        # directory of JSON files and is not against a database.
        #
        # Browsers request /favicon.ico on their own, and bookmark/history UI
        # still prefers it over the <link> tags. It is in PUBLIC_PATHS so the
        # request cannot redirect to /unlock.
        if path == "/favicon.ico":
            return self.send_file(STATIC_DIR / "favicon.ico")

        if path.startswith("/static/"):
            p = paths.safe_join(STATIC_DIR, path[len("/static/"):])
            if p is None:
                return self.send(403, "Forbidden", "text/plain")
            # The font subsets and the icon set are immutable: their bytes only
            # change when the filename does. Left on the server-wide no-store
            # default they were re-fetched on every single page load, which on
            # a real network is a visible flash of Georgia/system-sans before
            # the woff2 lands and `font-display: swap` repaints (invisible on
            # localhost, obvious once deployed). A year is the conventional
            # max-age for content-addressed assets; `immutable` tells the
            # browser not to even revalidate on reload.
            #
            # Deliberately NOT extended to .css/.js: those filenames are stable
            # across deploys, so caching them would pin a stale stylesheet
            # until someone hard-refreshed. They are render-blocking rather
            # than swap-in, so re-fetching them costs latency but never a
            # flash. Give them a content hash in the URL and they can join.
            if p.suffix.lower() in IMMUTABLE_STATIC_SUFFIXES:
                return self.send_file(p, extra_headers=[
                    ("Cache-Control", "public, max-age=31536000, immutable")])
            return self.send_file(p)

        owner = self.current_user()
        if path == "/getting-started":
            return self.send(200, getting_started_page(owner))

        if path == "/extension":
            return self.send(200, extension_page(owner))

        # {id: row}, account-private, re-read each request. Filtered in the
        # query, so no other member's row ever enters this process.
        walkthroughs = store.load_walkthroughs(owner)

        if path == "/":
            return self.send(200, catalog_page(walkthroughs, owner))

        if path == "/branding":
            return self.send(200, branding_page(owner))

        if path == "/account":
            return self.send(200, account_page(owner))

        if path == "/usage":
            period = parse_qs(urlparse(self.path).query).get(
                "days", [USAGE_DEFAULT_PERIOD])[0]
            return self.send(200, usage_page(owner, period))

        if path == "/api/branding":
            return self.send_json(branding.branding_collection(owner))

        # Collections are per-account, so membership is read from here rather
        # than folded into walkthrough_summary — that would re-query once per
        # walkthrough and duplicate the source of truth.
        #
        # No known-id set is passed any more. It existed to filter out members
        # naming a walkthrough that had been deleted; `collection_items` now has
        # a foreign key with `on delete cascade`, so a dangling member is not
        # something that can exist. See the note in library.py.
        if path == "/api/collections":
            return self.send_json(
                {"collections": library.list_collections(owner)})

        if path == "/api/fonts":
            fonts, source = branding.fetch_google_fonts()
            return self.send_json({"fonts": fonts, "source": source})

        if path.startswith("/player/"):
            walkthrough_id = path.split("/", 2)[2].strip("/")
            if walkthrough_id not in walkthroughs:
                return self.no_such_walkthrough_page(walkthrough_id)
            # Edit mode only exists in the classic player, so the catalog
            # card's Edit link (?edit=1) keeps landing there. Plain playback
            # gets the popup player.
            if parse_qs(urlparse(self.path).query).get("edit") == ["1"]:
                return self.send(200, classic_player_page())
            return self.send(200, player_page())

        if path.startswith("/player1/"):
            walkthrough_id = path.split("/", 2)[2].strip("/")
            if walkthrough_id not in walkthroughs:
                return self.no_such_walkthrough_page(walkthrough_id)
            return self.send(200, classic_player_page())

        if path.startswith("/review/"):
            walkthrough_id = path.split("/", 2)[2].strip("/")
            if walkthrough_id not in walkthroughs:
                return self.no_such_walkthrough_page(walkthrough_id)
            return self.send(200, review_page(walkthrough_id, owner))

        if path == "/api/walkthroughs":
            return self.send_json([walkthrough_summary(row)
                                   for row in walkthroughs.values()])

        # What the catalog needs to know about this server and this member.
        if path == "/api/settings":
            return self.send_json(settings_json(owner))

        # ---- jobs: what is running, how it is going, and how to stop it ----
        # Listed before the /api/walkthroughs/ prefix block because a job is
        # addressed by its own id, not by a walkthrough.
        if path == "/api/jobs":
            # What this member has running right now. The catalog reads it on
            # load so a reopened tab re-attaches to a pass in flight instead
            # of showing an idle card with an "Author with AI" button that
            # would start a second one.
            return self.send_json([j.snapshot(include_events=False)
                                   for j in jobs.active() if self.owns_job(j)])

        if path.startswith("/api/jobs/"):
            rest = path.split("/", 3)[3].strip("/")
            streaming = rest.endswith("/events")
            job_id = rest[:-len("/events")].strip("/") if streaming else rest
            job = jobs.get(job_id)
            # One answer for "no such job" and "not yours": a job id is
            # guessable enough that distinguishing them leaks which ids exist.
            if job is None or not self.owns_job(job):
                return self.send_json({"error": "No such job."}, 404)
            if streaming:
                since = 0
                try:
                    since = int(parse_qs(urlparse(self.path).query)
                                .get("since", ["0"])[0])
                except ValueError:
                    pass
                return self.send_event_stream(job, since)
            return self.send_json(job.snapshot())

        if path.startswith("/api/walkthroughs/"):
            rest = path.split("/", 3)[3].strip("/")
            if any(rest.endswith(s) for s in EXPORT_SUFFIXES):
                return self.handle_export(rest, walkthroughs, owner)
            if rest.endswith("/logs"):   # the AI log the catalog menu opens
                walkthrough_id = rest[:-len("/logs")]
                if walkthrough_id not in walkthroughs:
                    return self.no_such_walkthrough(walkthrough_id)
                return self.send_json(diagnostics.owner_view(walkthrough_id))
            if rest.endswith("/embed"):   # embed publication state
                walkthrough_id = rest[:-len("/embed")]
                row = walkthroughs.get(walkthrough_id)
                if row is None:
                    return self.no_such_walkthrough(walkthrough_id)
                emb = store.load_embed_settings(row) or {}
                return self.send_json({
                    "enabled": bool(emb.get("enabled")),
                    "profile": emb.get("profile"),
                    "intro": emb.get("intro", True),
                    "chrome": emb.get("chrome") if emb.get("chrome") in ("full", "viewport") else "full",
                    "backdrop": emb.get("backdrop", True),
                    "max_width": emb.get("max_width"),
                    "path": f"/embed/{walkthrough_id}",
                })
            if rest.endswith("/thumb"):   # catalog card image
                walkthrough_id = rest[:-len("/thumb")]
                row = walkthroughs.get(walkthrough_id)
                if row is None:
                    return self.no_such_walkthrough(walkthrough_id)
                # Bytes, not a path: the frame is an object in the captures folder, so
                # there is no file to sendfile() and something has to carry it.
                thumb = thumbs.thumbnail(walkthrough_id, row["definition"])
                if thumb is None:
                    # Not every capture has a frame; the card draws a plate.
                    return self.send(404, "No capture frame", "text/plain")
                # The one route that opts out of no-store. A shelf of cards is
                # rebuilt from scratch on every 10s poll, so without this every
                # tick refetches every thumbnail. The frame behind a given id
                # only changes on a render check (an id is never reused, so a
                # re-capture is a different walkthrough now), and ten minutes of
                # staleness on a catalog image is not a wrong answer in the way
                # a stale balance or a stale draft would be.
                #
                # The ETag is what makes the eleventh minute cheap. Without it
                # an expired entry costs the whole image again, because there
                # is nothing for the browser to ask *about* — with it the
                # question is a header and the answer is 304 and no body, and
                # thumbs.py answers it out of memory without touching storage.
                cache = [("Cache-Control", "private, max-age=600"),
                         ("ETag", thumb.etag)]
                if self.if_none_match(thumb.etag):
                    return self.send_not_modified(cache)
                return self.send(200, thumb.data, thumb.content_type,
                                 extra_headers=cache)
            if rest.endswith("/render-check"):   # stored verdicts, if any
                walkthrough_id = rest[:-len("/render-check")]
                row = walkthroughs.get(walkthrough_id)
                if row is None:
                    return self.no_such_walkthrough(walkthrough_id)
                # Off the row already in hand — the verdicts are one of its
                # columns, so render_check.load_results() would re-fetch it.
                return self.send_json(store.load_render_check(row)
                                      or {"pages": None})
            if rest.endswith("/verify-replay"):   # stored replay verdicts, if any
                walkthrough_id = rest[:-len("/verify-replay")]
                row = walkthroughs.get(walkthrough_id)
                if row is None:
                    return self.no_such_walkthrough(walkthrough_id)
                return self.send_json(verify_pass.load_results(row)
                                      or {"steps": None})
            if rest.endswith("/pages"):   # snapshot list for the editor
                walkthrough_id = rest[:-len("/pages")]
                if walkthrough_id not in walkthroughs:
                    return self.no_such_walkthrough(walkthrough_id)
                pages = [rel for rel, _ in author.snapshot_pages(walkthrough_id)]
                # auto/ snapshots are usable pages too. A prefix test on the
                # object listing where this walked a directory — the captures folder has
                # no directories, and `auto/x.html` is a key, not a file in a
                # folder.
                pages += [rel for rel in store.capture_files(walkthrough_id, ".html")
                          if rel.startswith("auto/")]
                return self.send_json(pages)
            row = walkthroughs.get(rest)
            if row is None:
                return self.no_such_walkthrough(rest)
            walkthrough = dict(row["definition"] or {})
            # Ownership is the `owner_id` column and never travels inside the
            # definition. This pops nothing on a row written since the move; it
            # stays because a definition carried over from the file era may
            # still hold the author's address.
            walkthrough.pop("owner", None)
            return self.send_json(walkthrough)

        # Captured pages are SingleFile snapshots of arbitrary third-party
        # sites with their JS inlined, and they are served here SAME-ORIGIN,
        # inside the player's iframe. That JS can therefore reach this origin's
        # cookies and same-origin APIs (POST /api/token, DELETE
        # /api/walkthroughs). capture_html_bytes injects export.FREEZE_SHIM
        # here like the export path always did — network primitives hang — but
        # the shim is a mitigation, not a boundary: page JS still runs
        # same-origin and DOM/timer access remains.
        #
        # sandbox="allow-scripts allow-forms" on the player iframe (omitting
        # allow-same-origin) is the obvious fix and was trialled, then backed
        # out as UNVERIFIED rather than known-broken: the page renders and
        # "Show me" completes end to end under it, and the player only ever
        # uses postMessage (never contentDocument), so bridge.js should be
        # unaffected. "You try" click validation could not be confirmed either
        # way in automation, because switching modes re-opens the intro modal
        # and it kept swallowing the test clicks. Verify by hand before
        # enabling; serving /capture/ from a separate origin would be stronger.
        if path.startswith("/capture/"):
            parts = path.split("/", 3)
            if len(parts) < 4:
                return self.send(404, "Not found", "text/plain")
            walkthrough_id, rel = parts[2], parts[3]
            if walkthrough_id not in walkthroughs:
                return self.no_such_walkthrough_page(walkthrough_id)
            # `paths.safe_join` is gone with the capture folder. Containment is
            # `blobs.key_for`'s job now, in one place rather than the five
            # independent copies the disk version had — and it returns None for
            # an unsafe path, so None here means both "escapes the capture" and
            # "no such object". That collapses what used to be a 403 and a 404
            # into one 404, which is the right answer either way: neither tells
            # the caller anything they did not already know.
            body = store.capture_bytes(walkthrough_id, rel)
            if body is None:
                return self.send(404, "Not found", "text/plain")
            # `blobs.content_type_for` rather than this module's MIME table: it
            # is the type the object was STORED under, so the response type and
            # the stored type cannot drift apart.
            ctype = blobs.content_type_for(rel)
            if ctype.startswith("text/html"):
                body = capture_html_bytes(body)
            return self.send(200, body, ctype)

        return self.send(404, "Not found", "text/plain")

    def write_refused(self, path):
        """Host and cross-site checks shared by POST, PUT and DELETE. Answers
        and returns True when the request must go no further."""
        if not self.host_allowed():
            self.refuse_request(421, "This server does not answer to that host name.")
            return True
        if self.cross_site_write(path):
            self.refuse_request(403, "Cross-site request refused.")
            return True
        return False

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if self.write_refused(path):
            return
        if path == "/unlock":
            return self.handle_unlock()
        # Pairing: the extension redeems a one-time code here and has no other
        # credential to present, so this sits ABOVE the authorized() gate. The
        # code is the authentication — single use, 120s, and minted only for a
        # browser page at /api/extension/pair.
        if path == "/api/extension/claim":
            return self.handle_extension_claim()
        if not self.authorized():
            return self.reject_unauthorized()
        if path == "/api/extension/pair":
            return self.handle_extension_pair()
        if path == "/api/token":
            return self.handle_token_generate()
        if path == "/api/upload":
            return self.handle_upload()
        if path == "/api/author":
            return self.handle_author()
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            return self.handle_job_cancel(
                path.split("/", 3)[3][:-len("/cancel")].strip("/"))
        if path == "/api/branding/save":
            return self.handle_branding_save()
        if path == "/api/branding/delete":
            return self.handle_branding_delete()
        if path == "/api/branding/select":
            return self.handle_branding_select()
        if path == "/api/branding/from-url":
            return self.handle_branding_from_url()
        if path.startswith("/api/walkthroughs/") and path.endswith("/ai-edit"):
            return self.handle_ai_edit(path.split("/", 3)[3][:-len("/ai-edit")].strip("/"))
        if path.startswith("/api/walkthroughs/") and path.endswith("/render-check"):
            return self.handle_render_check(
                path.split("/", 3)[3][:-len("/render-check")].strip("/"))
        if path.startswith("/api/walkthroughs/") and path.endswith("/verify-replay"):
            return self.handle_verify_replay(
                path.split("/", 3)[3][:-len("/verify-replay")].strip("/"))
        if path.startswith("/api/walkthroughs/") and path.endswith("/restore"):
            return self.handle_restore(path.split("/", 3)[3][:-len("/restore")].strip("/"))
        if path.startswith("/api/walkthroughs/") and path.endswith("/reset-draft"):
            return self.handle_reset_draft(
                path.split("/", 3)[3][:-len("/reset-draft")].strip("/"))
        if path.startswith("/api/walkthroughs/") and path.endswith("/embed"):
            return self.handle_embed_save(path.split("/", 3)[3][:-len("/embed")].strip("/"))
        # One walkthrough's membership across every collection, in one write.
        if path.startswith("/api/walkthroughs/") and path.endswith("/collections"):
            return self.handle_membership_save(
                path.split("/", 3)[3][:-len("/collections")].strip("/"))
        if path == "/api/collections":
            return self.handle_collection_create()
        # POST rather than PUT for the per-collection edits: do_PUT routes the
        # whole remainder of /api/walkthroughs/… to the editor save with no
        # suffix matching, and only do_POST has suffix dispatch at all. Keeping
        # every collection write on POST keeps one convention.
        if path.startswith("/api/collections/") and path.endswith("/items"):
            return self.handle_collection_items(
                path.split("/", 3)[3][:-len("/items")].strip("/"))
        if path.startswith("/api/collections/"):
            return self.handle_collection_update(path.split("/", 3)[3].strip("/"))
        return self.send(404, "Not found", "text/plain")

    # ---- render check: capture screenshot vs live player render, on demand --
    def handle_render_check(self, walkthrough_id):
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)
        # Self-address the bound socket directly rather than trusting the
        # Host header: behind a proxy the public name may not be one this
        # process can reach itself on. The session cookie is forwarded so
        # the headless browser passes the same auth gate the player does.
        port = self.server.server_address[1]
        cookie = self.headers.get("Cookie")

        def work(job):
            return render_check.run_check(
                walkthrough_id, f"http://127.0.0.1:{port}",
                cookie_header=cookie, progress=job.progress)

        return self.run_as_job(
            walkthrough_id, "render-check", work,
            background=self.wants_background(),
            soft_errors=(render_check.RenderCheckError, author.AuthorError))

    def handle_verify_replay(self, walkthrough_id):
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)
        # Same self-addressing as the render check: the headless browser
        # talks to this very server with the caller's session cookie.
        port = self.server.server_address[1]
        cookie = self.headers.get("Cookie")

        def work(job):
            return verify_pass.run_verify(
                walkthrough_id, f"http://127.0.0.1:{port}",
                cookie_header=cookie, progress=job.progress)

        return self.run_as_job(
            walkthrough_id, "verify-replay", work,
            background=self.wants_background(),
            soft_errors=(verify_pass.VerifyError, author.AuthorError))

    # ---- embed publication: enable/disable the public /embed/<id> URL ----
    def handle_embed_save(self, walkthrough_id):
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)
        payload = self.read_json()
        if not isinstance(payload, dict):
            return self.send_json({"error": "Expected a JSON body"}, 400)
        profile = payload.get("profile")
        chrome = payload.get("chrome")
        if chrome not in ("full", "viewport"):
            chrome = "full"
        max_width = payload.get("max_width")
        try:
            max_width = int(max_width) if max_width not in (None, "") else None
        except (TypeError, ValueError):
            max_width = None
        if max_width is not None:
            max_width = max(240, min(2000, max_width))  # sane bounds either side
        settings = {
            "enabled": bool(payload.get("enabled")),
            "profile": profile if isinstance(profile, str) and profile else None,
            "intro": bool(payload.get("intro", True)),
            "chrome": chrome,
            "backdrop": bool(payload.get("backdrop", True)),
            "max_width": max_width,
        }
        # `updated_at` is stamped by store.save_embed_settings, in UTC, in one
        # place. It used to be built here with a naive local `datetime.now()`,
        # which meant the timestamp on a published embed depended on the
        # server's timezone.
        store.save_embed_settings(walkthrough_id, settings)
        state = "published for embedding" if settings["enabled"] else "embed disabled"
        print(f"[embed] {walkthrough_id} {state}")
        return self.send_json({"ok": True, **settings,
                               "path": f"/embed/{walkthrough_id}"})

    # ---- collections: named, ordered sets of walkthroughs ----------------
    # Every write here goes through library.py, which owns the `collections`
    # and `collection_items` tables. See that module for why the collection
    # holds its members rather than the other way round.

    def collections_body(self):
        """Shared preamble for the collection mutation handlers.

        Returns (body, owner), or None after having already sent the error
        response.

        The known-id set this used to return with them is gone. It was the
        caller's half of a dangling-member filter that `collection_items`'
        foreign key now enforces, and every write below already checks
        ownership of the ids it is handed inside its own query — so passing a
        set of ids alongside was a second, weaker copy of the same rule.

        A browser session is required, not `authorized()`: the capture
        extension's bearer token has no business reordering someone's library,
        and narrowing what a leaked token can reach costs nothing here.
        """
        user = self.current_user()
        if not user:
            self.send_json({"error": "Unlock this server at /unlock to manage collections."}, 401)
            return None
        body = self.read_json()
        if not isinstance(body, dict):
            self.send_json({"error": "Expected a JSON body"}, 400)
            return None
        return body, user

    def library_error(self, exc):
        """A LibraryError as a response.

        `library` raises one exception for two situations that want different
        statuses: a write it refused (400) and a collection or walkthrough that
        is not this member's (404). It used to raise KeyError for the second,
        which is why the handlers below each had a bare `except KeyError`; the
        store rewrite collapsed both into LibraryError, so the distinction is
        drawn once, here, on the message the module itself wrote.
        """
        message = str(exc)
        return self.send_json({"error": message},
                              404 if message.startswith("No such") else 400)

    def _collections_payload(self, owner, status=200, **extra):
        """Every mutation answers with the whole collection list, so the client
        never has to merge a partial response into what it already had."""
        return self.send_json(
            {"ok": True, "collections": library.list_collections(owner),
             **extra}, status)

    def handle_collection_create(self):
        parsed = self.collections_body()
        if parsed is None:
            return
        body, owner = parsed
        try:
            collection = library.create_collection(
                owner, body.get("name"), body.get("description", ""))
        except library.LibraryError as e:
            # a cap is a "you already have enough", not a malformed request
            status = 409 if "at most" in str(e) else 400
            return self.send_json({"error": str(e)}, status)
        print(f"[collections] created {collection['id']}")
        return self._collections_payload(owner, collection=collection, status=201)

    def handle_collection_update(self, collection_id):
        parsed = self.collections_body()
        if parsed is None:
            return
        body, owner = parsed
        try:
            collection = library.update_collection(
                owner, collection_id,
                name=body.get("name"), description=body.get("description"))
        except library.LibraryError as e:
            return self.library_error(e)
        return self._collections_payload(owner, collection=collection)

    def handle_collection_items(self, collection_id):
        """Replace one collection's ordered membership — this is both reorder
        and add/remove, so the write is idempotent."""
        parsed = self.collections_body()
        if parsed is None:
            return
        body, owner = parsed
        try:
            collection = library.set_items(
                owner, collection_id, body.get("walkthroughs"))
        except library.LibraryError as e:
            return self.library_error(e)
        return self._collections_payload(owner, collection=collection)

    def handle_collection_delete(self, collection_id):
        user = self.current_user()
        if not user:
            return self.send_json({"error": "Unlock this server at /unlock to manage collections."},
                                  401)
        try:
            library.delete_collection(user, collection_id)
        except library.LibraryError as e:
            return self.library_error(e)
        print(f"[collections] deleted {collection_id}")
        # the walkthroughs themselves are untouched — only the grouping goes
        return self._collections_payload(user, deleted=collection_id)

    def handle_membership_save(self, walkthrough_id):
        parsed = self.collections_body()
        if parsed is None:
            return
        body, owner = parsed
        try:
            library.set_membership(owner, walkthrough_id, body.get("collections"))
        except library.LibraryError as e:
            # `set_membership` checks ownership of the walkthrough itself, so
            # the membership test this handler used to do first is gone: it was
            # the same question asked twice, once against a set built from a
            # full catalog read.
            return self.library_error(e)
        return self._collections_payload(owner)

    # ---- branding profiles: create / update / delete / select ------------
    def handle_branding_save(self):
        """Create or update an editable profile. The read-only my-brand
        profile can never be written."""
        parsed = self.branding_body()
        if parsed is None:
            return
        body, pid, owner = parsed

        clean, errors = branding.validate_branding(body)
        if errors:
            return self.send_json({"error": "Not saved", "errors": errors}, 422)

        # Update in place when an id was sent, insert otherwise. The
        # read-modify-write over a whole profile file is gone, and with it the
        # documented race that lost one of two simultaneous saves: both calls
        # touch exactly one row and take their own lock. The owner filter is
        # inside the UPDATE, so there is no window between checking ownership
        # and writing.
        if pid:
            profile = branding.update_profile(owner, pid, clean)
            if profile is None:
                return self.send_json({"error": f"Unknown profile: {pid}"}, 404)
        else:
            profile = branding.create_profile(owner, clean)
            if profile is None:
                return self.send_json(
                    {"error": f"You can have at most {branding.MAX_PROFILES} "
                              "brand profiles."}, 409)
        # Saving a profile also selects it, as it always has.
        branding.set_active_profile(owner, profile["id"])
        print(f"[branding] profile saved: {profile['id']}")
        return self.send_json({"ok": True, "id": profile["id"],
                               "collection": branding.branding_collection(owner)})

    def handle_branding_delete(self):
        parsed = self.branding_body()
        if parsed is None:
            return
        _body, pid, owner = parsed
        if not branding.delete_profile(owner, pid):
            return self.send_json({"error": f"Unknown profile: {pid}"}, 404)
        # No "was this the active one?" branch. `members.active_brand_profile_id`
        # is a foreign key with `on delete set null`, so deleting the active
        # profile resets the member to the built-in one in the same statement —
        # for every reader at once, rather than whenever this code remembered.
        print(f"[branding] profile deleted: {pid}")
        return self.send_json({"ok": True, "collection": branding.branding_collection(owner)})

    def handle_branding_select(self):
        # selecting my-brand IS allowed here, unlike save/delete
        parsed = self.branding_body(reject_mybrand=False)
        if parsed is None:
            return
        _body, pid, owner = parsed
        # `set_active_profile` resolves an id that is not this member's to the
        # built-in profile rather than to somebody else's brand, and returns
        # what it actually selected — so the response says what happened
        # instead of echoing what was asked for.
        return self.send_json({"ok": True,
                               "active": branding.set_active_profile(owner, pid)})

    # ---- import: fetch a URL + AI-infer a starting brand profile ---------
    def handle_branding_from_url(self):
        """Fetch a company's website and ask AI to infer a starting brand
        profile (colours, fonts, logo/favicon, name, tagline) for the form.
        Never saves anything — the caller reviews the result and Saves it."""
        body = self.read_json()
        if body is None or not isinstance(body, dict):
            return self.send_json({"error": "Expected a JSON body"}, 400)
        url = str(body.get("url", "")).strip()
        if not url:
            return self.send_json({"error": "Enter a URL first."}, 400)
        if "://" not in url:
            url = "https://" + url
        try:
            with usage.attribute("brand-import", member_id=self.request_user()):
                data = brandfetch.infer_branding_from_url(url)
        except author.AuthorError as e:
            return self.send_json({"error": str(e)}, 422)
        except Exception as e:
            if DEBUG:
                traceback.print_exc()
            return self.send_json({"error": f"Could not analyze that URL: {e}"}, 500)
        clean, errors = branding.validate_branding(data)
        source_url = data.get("source_url", url)
        print(f"[branding] inferred profile from {source_url}"
              + (f" ({len(errors)} field(s) fell back to defaults)" if errors else ""))
        return self.send_json({"ok": True, "brand": clean, "source_url": source_url})

    # ---- ai-edit: published walkthrough + instruction → AI edit ----------------
    def handle_ai_edit(self, walkthrough_id):
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)

        body = self.read_json()
        if not isinstance(body, dict):
            return self.send_json({"error": "Expected a JSON body"}, 400)
        instruction = str(body.get("instruction", "")).strip()
        if not instruction:
            return self.send_json({"error": "Describe the change first."}, 400)
        current_step = body.get("current_step")
        if not isinstance(current_step, int) or isinstance(current_step, bool):
            current_step = None
        selection = clean_selection(body.get("selection"))
        # Same self-addressing as the replay check: the edit loop's probe
        # tool and post-save sweep drive a headless browser at this very
        # server with the caller's session cookie.
        port = self.server.server_address[1]
        cookie = self.headers.get("Cookie")
        member = self.request_user()
        # For the diagnostic log: the request exactly as the author sent it,
        # before the stripping and coercion above.
        logged_request = {k: body.get(k) for k in
                          ("instruction", "current_step", "selection")}

        def work(job):
            # Inside the job, so the log's before/after reads are covered by
            # the same exclusion as the edit itself.
            started = datetime.now(timezone.utc)
            prior = diagnostics.history_stamps(walkthrough_id)
            try:
                result = author.run_edit(walkthrough_id, instruction,
                                         current_step=current_step,
                                         selection=selection,
                                         progress=job.progress,
                                         replay_ctx=(f"http://127.0.0.1:{port}",
                                                     cookie))
            except BaseException as e:
                diagnostics.record_ai_edit(walkthrough_id, member, logged_request,
                                           started, prior, error=e)
                raise
            diagnostics.record_ai_edit(walkthrough_id, member, logged_request,
                                       started, prior, result=result)
            if result.get("kind") == "answer":
                usage.relabel(job.id, "ai-assist")
            # The editor needs the definition itself, which the result does not
            # carry. Re-read the row rather than trusting the in-memory copy:
            # the post-save probe sweep runs between the write and here, and
            # this is the definition the next GET would return.
            d = dict(store.require(walkthrough_id)["definition"] or {})
            d.pop("owner", None)   # never leak account metadata to the editor
            return {"walkthrough": d,
                    # "edit" or "answer" — an answer saved nothing, so the
                    # editor shows it as a reply and offers no undo
                    "kind": result.get("kind", "edit"),
                    "answer": result.get("answer", ""),
                    "suggested_change": result.get("suggested_change", ""),
                    "steps_before": result["steps_before"],
                    "steps_after": result["steps_after"],
                    "pages_changed": result["pages_changed"],
                    # which pages, so the editor can point at the steps on them
                    "pages_changed_paths": result.get("pages_changed_paths", []),
                    "warnings": result["warnings"],
                    "summary": result.get("summary", ""),
                    "stamp": result["stamp"]}

        return self.run_as_job(walkthrough_id, "ai-edit", work,
                               background=self.wants_background(),
                               soft_errors=(author.AuthorError,))

    # ---- restore: put a history snapshot (walkthrough + pages) back live -------
    def handle_restore(self, walkthrough_id):
        """Make one saved version live again.

        The history entry used to be `history/walkthrough-<stamp>.json` beside
        a `history/pages-<stamp>/` folder, walked here with `rglob`. It is a
        `walkthrough_history` row now: `definition` is the version, and
        `pages_prefix` names where the capture pages it was written against
        were backed up in the captures folder. So this reads one row and lists one
        prefix, and the traversal check that guarded the folder walk lives in
        `blobs.key_for`, which the reads below go through.
        """
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)

        body = self.read_json()
        stamp = str((body or {}).get("stamp", "")).strip()
        # Still validated before it goes anywhere. It is a query parameter now
        # rather than half a filename, so this is no longer a traversal guard —
        # but a stamp that cannot match store.STAMP_FORMAT is a malformed
        # request, and a plain 400 says so better than an empty 404 would.
        if not STAMP_RE.match(stamp):
            return self.send_json({"error": "A history stamp is required"}, 400)

        try:
            job = jobs.claim(walkthrough_id, "restore", owner=self.request_user(),
                             timeout=SHORT_JOB_TIMEOUT)
        except jobs.JobBusy as busy:
            return self.job_busy(busy)
        try:
            entry = store.history_entry(walkthrough_id, stamp)
            if entry is None:
                return self.send_json({"error": f"No history entry {stamp}"}, 404)

            restored = dict(entry["definition"] or {})
            # Same rule as the editor save: identity and wiring stay put. Two
            # of the four fields this used to pin are gone — `capture_dir`,
            # because the capture prefix is the id, and `owner`, because
            # ownership is a column and never travels in the definition.
            restored["schema"] = "walkthrough-def@1"
            restored["id"] = walkthrough_id
            restored.pop("capture_dir", None)
            restored.pop("owner", None)
            restored.pop("_source", None)

            # The page backups taken when this version was superseded. Keyed by
            # the capture-relative path, which is what save_with_history writes
            # back — the same string on both sides, where the folder version
            # had to convert a Path into one.
            changed = {}
            prefix = entry["pages_prefix"]
            if prefix:
                for key in blobs.list_keys(prefix):
                    rel = key[len(prefix):]
                    if rel.lower().endswith(".html"):
                        changed[rel] = blobs.get_text(key)

            # No re-validation: the snapshot was valid when it was live, and
            # walkthrough + pages come from the same stamp. Writing through
            # save_with_history makes the restore itself undoable.
            try:
                new_stamp = store.save_with_history(
                    walkthrough_id, restored, changed_pages=changed or None)
            except (blobs.StorageError, store.NoSuchWalkthrough) as e:
                if DEBUG:
                    traceback.print_exc()
                return self.send_json({"error": f"Restore failed: {e}"}, 500)
        finally:
            job.finish()
            jobs.release(job)

        print(f"[restore] {walkthrough_id} restored from {stamp} "
              f"({len(changed)} page(s))")
        return self.send_json({"ok": True,
                               "walkthrough": restored,
                               "stamp": new_stamp,
                               "restored_from": stamp,
                               "pages_restored": len(changed)})

    # ---- reset to draft: undo the authoring pass, back into the queue --------
    def handle_reset_draft(self, walkthrough_id):
        """Return an authored walkthrough to its pre-authoring draft.

        The authoring pass refuses a walkthrough that has been through it, and a
        history restore brings back an old definition without clearing
        `authored_at` — so without this, re-testing the pass on a capture meant
        recording it again.
        """
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)
        try:
            job = jobs.claim(walkthrough_id, "reset", owner=self.request_user(),
                             timeout=SHORT_JOB_TIMEOUT)
        except jobs.JobBusy as busy:
            return self.job_busy(busy)
        try:
            stamp, pages = store.reset_to_draft(walkthrough_id)
        except store.NoSuchWalkthrough:
            return self.no_such_walkthrough(walkthrough_id)
        except store.NoDraft:
            return self.send_json(
                {"error": "This walkthrough has no pre-authoring draft to go back to."}, 409)
        except blobs.StorageError as e:
            if DEBUG:
                traceback.print_exc()
            return self.send_json({"error": f"Reset failed: {e}"}, 500)
        finally:
            job.finish()
            jobs.release(job)
        print(f"[reset] {walkthrough_id} back to its draft ({pages} page(s) restored)")
        return self.send_json({"ok": True, "walkthrough_id": walkthrough_id,
                               "stamp": stamp, "pages_restored": pages})

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        if self.write_refused(path):
            return
        if not self.authorized():
            return self.reject_unauthorized()
        if path.startswith("/api/walkthroughs/"):
            return self.handle_put_walkthrough(path.split("/", 3)[3].strip("/"))
        return self.send(404, "Not found", "text/plain")

    # ---- save: validated walkthrough write from the in-player editor -----------
    def handle_put_walkthrough(self, walkthrough_id):
        row = self.my_walkthrough(walkthrough_id)
        if row is None:
            return self.no_such_walkthrough(walkthrough_id)
        walkthrough = self.read_json()
        # read_json() returns {} for an empty body; an empty object is not a
        # walkthrough, so it must still be rejected here.
        if not isinstance(walkthrough, dict) or not walkthrough:
            return self.send_json({"error": "Expected the walkthrough JSON as the body"}, 400)

        # The save holds the walkthrough for its whole read-validate-write,
        # not just for the length of a membership test. Previously this
        # checked the AUTHORING set and released the lock immediately, so an
        # AI pass starting one line later interleaved with `save_with_history`
        # — which rewrites the definition *and* the captured pages.
        try:
            job = jobs.claim(walkthrough_id, "save", owner=self.request_user(),
                             timeout=SHORT_JOB_TIMEOUT)
        except jobs.JobBusy as busy:
            return self.job_busy(busy)
        try:
            # The editor may not tamper with identity or wiring. Two of the
            # four pinned fields are gone: `capture_dir` is not in the schema
            # any more (the prefix is the id), and `owner` is the `owner_id`
            # column, which no request body can reach.
            walkthrough["schema"] = "walkthrough-def@1"
            walkthrough["id"] = walkthrough_id
            walkthrough.pop("capture_dir", None)
            walkthrough.pop("owner", None)
            walkthrough.pop("_source", None)

            pages = author.snapshot_pages(walkthrough_id)
            errors, warnings = author.validate(
                walkthrough, walkthrough_id, {rel for rel, _ in pages},
                render_notes=author.render_notes(store.load_render_check(row)))
            if errors:
                return self.send_json({"error": "Not saved: " + "; ".join(errors)}, 422)

            # No `current_raw` third argument: save_with_history reads the
            # previous definition itself, under a row lock inside its own
            # transaction. Passing the copy read up here meant the version that
            # went into history was whatever this request happened to have
            # loaded, which is not necessarily the one being replaced.
            stamp = store.save_with_history(walkthrough_id, walkthrough)
        finally:
            job.finish()
            jobs.release(job)
        print(f"[save] {walkthrough_id} saved from editor "
              f"({len(warnings)} warning(s))")
        # The stamp, not a path: a history entry is a row addressed by
        # (walkthrough, stamp), and it is the stamp /restore takes.
        return self.send_json({"ok": True, "warnings": warnings, "stamp": stamp})

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        if self.write_refused(path):
            return
        if not self.authorized():
            return self.reject_unauthorized()
        if path.startswith("/api/extension/devices/"):
            return self.handle_device_revoke(
                path[len("/api/extension/devices/"):].strip("/"))
        if path.startswith("/api/collections/"):
            return self.handle_collection_delete(path.split("/", 3)[3].strip("/"))
        if path.startswith("/api/walkthroughs/"):
            return self.handle_delete(path.split("/", 3)[3].strip("/"))
        return self.send(404, "Not found", "text/plain")

    # ---- delete: the walkthrough row + every object in its capture prefix -----
    def handle_delete(self, walkthrough_id):
        if not walkthrough_id:
            return self.send_json({"error": "A walkthrough id is required"}, 400)
        if not self.owns_walkthrough(walkthrough_id):
            return self.no_such_walkthrough(walkthrough_id)
        # Same fix as the editor save: hold the walkthrough for the whole
        # delete. A destructive sweep interleaved with a running pass was
        # previously reachable — the check released its lock before the first
        # object went.
        try:
            job = jobs.claim(walkthrough_id, "delete", owner=self.request_user(),
                             timeout=SHORT_JOB_TIMEOUT)
        except jobs.JobBusy as busy:
            return self.job_busy(busy)
        try:
            return self._delete_walkthrough(walkthrough_id)
        finally:
            job.finish()
            jobs.release(job)

    def _delete_walkthrough(self, walkthrough_id):
        try:
            removed = store.delete(walkthrough_id)
        except store.NoSuchWalkthrough:
            return self.no_such_walkthrough(walkthrough_id)
        # No collection prune afterwards. `collection_items.walkthrough_id`
        # references `walkthroughs(id) on delete cascade`, so a deleted
        # walkthrough leaves every collection in the same transaction — the
        # sweep this used to do, and the read-path filter that backed it up,
        # were two mechanisms for one invariant the database now holds. See
        # the note in library.py.
        print(f"[delete] removed {', '.join(removed)}")
        return self.send_json({"ok": True, "walkthrough_id": walkthrough_id, "removed": removed})

    # ---- capture token: manual fallback for the logged-in member --------------
    def handle_token_generate(self):
        """Mint a capture token for the logged-in member (shown once).

        Requires a browser session, not a bearer token — a leaked capture token
        must not be able to mint its own replacements.

        This is now the *fallback* path: pairing (below) is what /account leads
        with. It survives because a deployment behind a proxy that rewrites
        pages, or a browser without the extension's messaging, still needs a way
        in — and because scripts use it. Unlike before, it ADDS a device rather
        than replacing the member's only token.
        """
        user = self.current_user()
        if not user:
            return self.send_json(
                {"error": "Unlock this server at /unlock to manage your capture token."}, 401)
        label = ""
        body = self.read_json()
        if isinstance(body, dict):
            label = str(body.get("label") or "")
        token, device_id = auth.generate_api_token(user, label or "Manual setup")
        if not token:
            return self.send_json({"error": "Account not found."}, 404)
        device = next((d for d in auth.list_devices(user) if d["id"] == device_id), {})
        return self.send_json({"ok": True, "token": token, "device_id": device_id,
                               "created": _stamp(device.get("created_at"))}, 201)

    # ---- extension pairing: offer / claim / revoke ----------------------------
    #
    # The flow and its rationale are in PAIRING.md. In short: the signed-in page
    # mints a one-time code (same-origin, cookie-authed), hands it to the
    # extension over postMessage, and the extension redeems it for a durable
    # per-device token. No token is ever displayed, and no CORS is needed —
    # the privileged call is same-origin and the redeem is extension→server,
    # exactly like /api/upload.
    def handle_extension_pair(self):
        """Mint a one-time pairing code for the logged-in member.

        Session only, for the same reason handle_token_generate is: a capture
        token that could mint pairing codes could mint itself an unlimited
        supply of replacements, and revoking the device would not stop it.
        """
        user = self.current_user()
        if not user:
            return self.send_json(
                {"error": "Unlock this server at /unlock to connect the extension."}, 401)
        code, ttl = auth.issue_pairing_code(user)
        return self.send_json({"ok": True, "code": code, "expires_in": ttl}, 201)

    def handle_extension_claim(self):
        """Redeem a pairing code for a per-device capture token.

        Reachable without a session — the extension has no cookie for this
        origin and that is the problem being solved. The code is the credential.

        Expired, already-claimed and never-issued all answer identically:
        telling them apart tells a guesser which half of the space to search,
        and no legitimate user behaves differently in the three cases.
        """
        body = self.read_json()
        if not isinstance(body, dict):
            return self.send_json({"error": "Expected a JSON object."}, 400)
        code = str(body.get("code") or "").strip()
        label = str(body.get("label") or "")
        member_id, token, device_id = auth.redeem_pairing_code(code, label)
        if not token:
            return self.send_json(
                {"error": "That pairing code has expired or was already used. "
                          "Click Connect again on your Account page."}, 400)
        # The extension shows "connected as <address>", so it still gets one —
        # read off the member row that redeemed, rather than being the thing
        # the code resolved to. `email` stays the wire key: extensions already
        # installed read it.
        member = auth.get_member(member_id) or {}
        print(f"[pair] {member.get('email') or member_id} paired a device ({device_id})")
        return self.send_json({"ok": True, "token": token,
                               "email": member.get("email", ""),
                               "device_id": device_id}, 201)

    def handle_device_revoke(self, device_id):
        """Revoke one paired device. Session only: a capture token must not be
        able to revoke its siblings — or, having been stolen, to revoke the
        member's real browser and keep the account to itself."""
        user = self.current_user()
        if not user:
            return self.send_json(
                {"error": "Unlock this server at /unlock to manage paired devices."}, 401)
        if not auth.revoke_device(user, device_id):
            return self.send_json({"error": "No such device."}, 404)
        print(f"[pair] {user} revoked device {device_id}")
        return self.send_json({"ok": True, "device_id": device_id})

    # ---- upload: capture ZIP → walkthrough row → objects under <uuid>/ -------
    def handle_upload(self):
        """Turn an uploaded capture ZIP into a draft walkthrough.

        THE ORDER INVERTED, and it had to. It used to be: pick a unique id from
        the capture's title, extract to `captures/<id>/`, convert, write
        `walkthrough.json`, then stamp the owner into it. But the capture's
        prefix in the captures folder IS the walkthrough's id, and the id is assigned by
        the database — so the row has to exist before there is anywhere to put
        the bytes:

            manifest -> definition -> row -> extract into the row's prefix

        Two things fall out of that. The de-duplication loop is gone: ids are
        uuids, and the human-readable name is `store.create`'s slug, which it
        makes unique per owner. And the failure cleanup moved with the
        extraction: there is no half-written folder to rmtree, so a failed
        extract deletes the row it just created — which clears the prefix too.

        The trade-off is that a failure now costs an insert and a delete rather
        than nothing. That is the right way round: the alternative is naming
        the capture before knowing whether it can be stored, which is exactly
        how a capture folder came to carry a different name from the
        walkthrough pointing at it.
        """
        ctype = self.headers.get("Content-Type", "")
        boundary = re.search(r'boundary="?([^";]+)"?', ctype)
        if "multipart/form-data" not in ctype or not boundary:
            return self.send_json({"error": "Expected multipart/form-data"}, 400)
        length = self.content_length()
        if length <= 0:
            return self.send_json({"error": "Empty request body"}, 400)
        if length > MAX_UPLOAD:
            return self.send_json(
                {"error": f"Upload too large (max {MAX_UPLOAD // (1024 * 1024)} MB)"}, 413)

        fields = parse_multipart(self.rfile.read(length), boundary.group(1))

        zip_field = fields.get("capture_zip")
        if not zip_field or not zip_field[1]:
            return self.send_json({"error": "No ZIP file attached"}, 400)

        # Read and convert the manifest BEFORE anything is created. A ZIP that
        # is not a capture is then rejected without a row or an object to undo,
        # and `ingest.convert` writes nothing by construction.
        try:
            manifest = ingest.zip_manifest(zip_field[1])
            definition = ingest.convert(manifest)
            ingest.check_capture_limits(zip_field[1], manifest, definition)
        except ingest.CaptureTooLarge as e:
            return self.send_json({"error": str(e), "code": "capture_limit"}, 413)
        except (ValueError, zipfile.BadZipFile) as e:
            return self.send_json({"error": f"Bad capture ZIP: {e}"}, 400)

        # The title: an explicit field if one was sent (curl/scripts), else the
        # capture's own. The form field is still called `walkthrough_id`,
        # because that is what existing scripts send — but nothing can choose an
        # id any more, so what it now supplies is a NAME. `store.create` turns
        # it into a slug unique within this owner, and the id it returns is a
        # uuid. That is the only title-to-slug step left on this path, and it
        # names a walkthrough rather than addressing one.
        name_field = fields.get("walkthrough_id", (None, b""))
        title = name_field[1].decode("utf-8", "replace").strip()
        title = title or str(definition.get("title") or "").strip() or "Untitled capture"

        # Uploads belong to the member who made them — a logged-in browser
        # session, or the member resolved from the extension's capture token.
        # `owner_id` is NOT NULL, so there is no fallback account any more: this
        # route is behind the authorized() gate, and a request that got here
        # has a member.
        owner = self.request_user()
        recorded_at = str((definition.get("_source") or {}).get("recorded_at") or "").strip()
        row = store.create(owner, title, definition,
                           recorded_at=recorded_at
                           if ISO_TIMESTAMP_RE.match(recorded_at) else None)
        walkthrough_id = row["id"]

        try:
            objects = ingest.extract_capture_zip(zip_field[1], walkthrough_id)
        except (ValueError, zipfile.BadZipFile, blobs.StorageError) as e:
            # The row and whatever objects landed go together. A walkthrough
            # whose capture is half-extracted plays as a wall of missing pages,
            # and nothing would ever come back to finish it.
            try:
                store.delete(walkthrough_id)
            except Exception as cleanup:
                print(f"[warn] could not remove {walkthrough_id} after a failed "
                      f"extraction: {cleanup}")
            status = 502 if isinstance(e, blobs.StorageError) else 400
            return self.send_json({"error": f"Capture not stored: {e}"}, status)

        # The recorder's panel log, a separate form field rather than a ZIP
        # entry so it never lands in the capture prefix (and from there in
        # exports). Older recorders do not send it; a bad one is dropped with
        # a warning — diagnostics never fail the upload they describe.
        log_field = fields.get("capture_log")
        if log_field and log_field[1]:
            diagnostics.record_capture_log(
                walkthrough_id, owner, log_field[1],
                recorder=self.headers.get("X-Captcher-Recorder"))

        print(f"[upload] {walkthrough_id} ({row['slug']}): "
              f"{objects} object(s), {len(definition.get('steps', []))} draft step(s)")
        return self.send_json({
            "ok": True,
            "walkthrough_id": walkthrough_id,
            "slug": row["slug"],
            "steps": definition.get("steps", []),
            "objects": objects,
            "player_url": f"/player/{walkthrough_id}",
        }, 201)

    # ---- author: draft walkthrough + scenario intent → AI authoring pass -------
    def handle_author(self):
        body = self.read_json()
        if body is None:
            return self.send_json({"error": "Expected a JSON body"}, 400)

        walkthrough_id = str(body.get("walkthrough_id", "")).strip()
        scenario = str(body.get("scenario", "")).strip() or None  # optional: AI derives it
        if not walkthrough_id:
            return self.send_json({"error": "A walkthrough id is required"}, 400)
        row = self.my_walkthrough(walkthrough_id)
        if row is None:
            return self.no_such_walkthrough(walkthrough_id)
        # Refused here, before the job is claimed; run_pass refuses it too.
        if row.get("authored_at"):
            return self.send_json({"error": "This walkthrough is already authored. "
                                   "Reset it to its draft to author it again."}, 409)

        port = self.server.server_address[1]
        cookie = self.headers.get("Cookie")
        member = self.request_user()
        # The render pre-check is part of authoring now, not an option. It was
        # opt-in while its cost (a vision call per unique page) was unproven;
        # in practice a pass steered by render verdicts is simply the better
        # pass, and the checkbox's real effect was authors forgetting it and
        # paying with a walkthrough that spotlights blank space. A
        # `render_check` field in the request body is ignored, so older
        # catalog pages keep working.

        def work(job):
            # Every pass is logged, however it ends: the catalog card says
            # only whether it worked, and the AI log (GET …/logs) carries the
            # warnings, the full error and the progress trail.
            started = datetime.now(timezone.utc)
            try:
                result = author_pass(job)
            except BaseException as e:
                diagnostics.record_author(walkthrough_id, member, started,
                                          job.snapshot()["events"], error=e)
                raise
            diagnostics.record_author(walkthrough_id, member, started,
                                      job.snapshot()["events"], result=result)
            return result

        def author_pass(job):
            # Pre-check: render every page and judge it against its capture
            # screenshot BEFORE authoring, so the pass can steer around pages
            # that don't render (author.render_notes reads the verdicts back).
            # Advisory because a broken checker must not block authoring.
            # Skipped when verdicts already exist — a re-author should reuse
            # the reviewed ones rather than pay for them again. Read off the row this
            # request already holds; they are one of its columns.
            #
            # It runs inside this job rather than claiming its own: exclusion
            # is per walkthrough now, so the pre-check cannot race a separate
            # POST /render-check the way it used to.
            if store.load_render_check(row) is None:
                try:
                    render_check.run_check(
                        walkthrough_id, f"http://127.0.0.1:{port}",
                        cookie_header=cookie, progress=job.progress)
                except (render_check.RenderCheckError, author.AuthorError) as e:
                    print(f"[author] pre-check skipped for {walkthrough_id}: {e}")
                    # A silent skip reads as "the check passed".
                    job.progress("pre-check", f"skipped — {e}")
            result = author.run_pass(walkthrough_id, scenario,
                                     progress=job.progress)
            store.mark_authored(walkthrough_id)  # leaves the queue, joins the library
            return result

        return self.run_as_job(walkthrough_id, "author", work,
                               background=self.wants_background(),
                               soft_errors=(author.AuthorError,))

    def handle_job_cancel(self, job_id):
        job = jobs.get(job_id)
        if job is None or not self.owns_job(job):
            return self.send_json({"error": "No such job."}, 404)
        if not job.cancel():
            # Already finished. Not an error — the author clicked Stop as the
            # pass landed, and the honest answer is that it landed.
            return self.send_json(
                {"ok": True, "already_finished": True,
                 **job.snapshot(include_events=False)})
        print(f"[job] cancel requested for {job.kind} {job.walkthrough_id}")
        # Deliberately not 202-with-a-wait: cancellation lands at the pass's
        # next boundary (see jobs.Cancelled), so the honest response is "asked"
        # rather than "stopped". The caller watches the stream for the
        # terminal event.
        return self.send_json({"ok": True, "cancel_requested": True,
                               **job.snapshot(include_events=False)})

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


def walkthrough_summary(row):
    """One catalog card's worth of a walkthrough row.

    Takes the row rather than (id, definition, capture_root): the id, the title
    and the recorded-at timestamp are columns, and the capture root does not
    exist. Everything else still comes out of the definition, which is where
    the author's own words live.
    """
    d = row["definition"] or {}
    todos = json.dumps(d).count("TODO")
    state = store.authoring_state(row)
    return {
        "id": row["id"],
        "title": d.get("title") or row.get("title") or row["id"],
        "track": d.get("track"),
        "level": d.get("level"),
        "estimated_minutes": d.get("estimated_minutes"),
        "steps": len(d.get("steps", [])),
        "todos": todos,
        # authored → in the library; otherwise it sits in the authoring queue.
        # `authored_at` is a column, so it is authoritative; the todos==0
        # fallback backfills a walkthrough authored before the column existed.
        "authored": bool(state.get("authored_at")) or todos == 0,
        "authored_at": state.get("authored_at"),
        "captured_at": store.captured_at(row),
        "scenario": (d.get("intro") or {}).get("scenario", ""),
        # What the walkthrough teaches, for the catalog card. intro.scenario is
        # written in character ("You are Priya Raman, billing coordinator…"),
        # so the first line of it describes a person rather than a task;
        # completion.summary describes the task. `scenario` stays in the
        # payload — the player's intro screen and the search index both use it.
        "summary": (d.get("completion") or {}).get("summary", ""),
    }


# ---- page rendering -------------------------------------------------------
# All markup lives in templates/ as real .html files; see templates.py. Values
# are spliced in via __PLACEHOLDER__ markers, so embedded CSS/JS braces need no
# escaping. Callers escape their own values, exactly as before.

INK = "currentColor"  # Inherit accessible Ink/Paper from the active chrome.


def logo_svg(size=24, stroke=INK):
    """The Captcher mark (frame plus five stepping squares), with a
    caller-selectable frame colour.

    By default the frame inherits the chrome's foreground colour; the squares
    always keep their brand colours. White-label players apply their own logo
    through export.apply_branding.
    """
    return templates.render("_logo.svg", S=size, STROKE=stroke)


def toolbar(active="", owner=None):
    """The shared app shell: a left sidebar on wide screens, a top bar below
    900px. Carries the main navigation.

    The mark's frame takes currentColor, so it follows the Light/Dark choice;
    its five squares keep their brand colours on either ground."""
    def cls(name):
        return "nav-link active" if active == name else "nav-link"

    def cur(name):
        return ' aria-current="page"' if active == name else ""
    return templates.render(
        "_toolbar.html",
        LOGO=logo_svg(24),
        CLS_CATALOG=cls("catalog"),
        CLS_BRANDING=cls("branding"),
        CLS_EXTENSION=cls("extension"),
        CLS_GETTING_STARTED=cls("getting-started"),
        CLS_ACCOUNT=cls("account"),
        CLS_USAGE=cls("usage"),
        CUR_CATALOG=cur("catalog"),
        CUR_BRANDING=cur("branding"),
        CUR_EXTENSION=cur("extension"),
        CUR_GETTING_STARTED=cur("getting-started"),
        CUR_ACCOUNT=cur("account"),
        CUR_USAGE=cur("usage"),
    )


def _chrome(active, owner=None):
    """The shared chrome placeholders every signed-in page carries."""
    return {
        # Tokens and the top bar's own rules moved into
        # static/captcher.css, which every page links; these two loads
        # stay so the keys remain available to any template that still wants a
        # page-local override.
        "TOKENS_CSS": templates.load("_tokens.css"),
        "TOOLBAR_CSS": templates.load("_toolbar.css"),
        "TOOLBAR": toolbar(active, owner),
        "TOOLBAR_SCRIPT": templates.load("_toolbar.js"),
        # Appearance. Every app page carries it and therefore honours the
        # choice; the unlock page deliberately does not. It goes in the HEAD,
        # above the stylesheet, and must stay there — see the file.
        "THEME_HEAD": templates.load("_theme-head.html"),
    }


def distribution_brand(walkthrough_id, profile_id, owner_id):
    """The brand a distributed copy of a walkthrough carries: the requested
    profile, resolved against the owner's saved brands. Branding never
    touches the walkthrough itself, so restyling costs no AI."""
    return branding.resolve_branding_profile(profile_id, owner_id)


def published_example(slug):
    """The row behind /examples/<slug>, or None.

    Two gates, both required. The slug must be named in
    CAPTCHER_EXAMPLE_WALKTHROUGHS, and `store.get_public_by_slug` must
    resolve it — which it does only for a walkthrough published for embedding,
    exactly as a customer-site embed would be. Configuration alone never makes
    a private walkthrough public; a fresh install answers a plain 404.

    The publication test used to be an `enabled` check here plus a membership
    test against every walkthrough on the server. Both are inside
    get_public_by_slug now, along with the rule about what to do when two
    accounts publish the same slug — which is what turns "resolve a name with
    no owner to filter by" from a lookup into a decision worth having in one
    place.
    """
    if slug not in EXAMPLE_WALKTHROUGH_SLUGS:
        return None
    return store.get_public_by_slug(slug)


def example_page(slug):
    """The production popup player as a full-page, published example."""
    row = published_example(slug)
    if row is None:
        return None
    return export.build_example_export(row["id"])


def unlock_page(error=""):
    """The one page an access-token server shows before it is unlocked."""
    err_html = (f'<div class="notice notice--error">{escape(error)}</div>') if error else ""
    return templates.render("unlock.html", LOGO=logo_svg(24), ERROR=err_html)


def _stamp(value):
    """A timestamp column as a short readable string, or "".

    `db` hands these back as `datetime` objects where the JSON stores gave
    strings, and several of them are rendered straight into a page or a JSON
    response. One formatter, so every page that shows a time
    disagree about what a date looks like.
    """
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def device_rows(owner):
    """The paired-device table, or the empty state.

    Rendered server-side rather than fetched, so the page is complete on first
    paint and the list is right even with JS off — the Connect button needs JS,
    but knowing what is already paired should not.

    Every field is escaped: `label` is chosen by whoever redeemed the pairing
    code (`auth._clean_label` bounds its length and nothing else).
    """
    devices = auth.list_devices(owner)
    if not devices:
        return ('<p class="small muted" id="device-empty">No browsers connected yet.</p>')
    rows = []
    for d in devices:
        used = _stamp(d.get("last_used_at"))
        # "added X · last used Y" — last used is the field that answers "which
        # of these is this laptop?", so it is not hidden behind a hover.
        meta = f"added {escape(_stamp(d.get('created_at')) or '?')}"
        meta += f" · last used {escape(used)}" if used else " · never used"
        rows.append(
            f'<li class="device" data-device="{escape(d["id"], quote=True)}">'
            f'<div class="device-id"><strong>{escape(d["label"])}</strong>'
            f'<span class="small muted mono">{meta}</span></div>'
            f'<button class="button button--tertiary button--sm device-revoke" '
            f'type="button">Revoke</button></li>')
    return f'<ul class="devices" id="device-list">{"".join(rows)}</ul>'


def getting_started_page(owner=None):
    """The tutorial. Its recording limits are ingest's, so the guide cannot
    drift from what the server accepts."""
    return templates.render("getting-started.html", **_chrome("getting-started", owner),
                            MAX_STEPS=ingest.MAX_CAPTURE_STEPS,
                            MAX_PAGES=ingest.MAX_CAPTURE_PAGES)


def extension_page(owner=None):
    """Setup guide for the recorder. It names the extension folder to load,
    so nobody has to go looking for it (install.extension_folder)."""
    from . import install
    folder = install.extension_folder()
    where = ""
    if (folder / "manifest.json").is_file():
        where = (f'On this computer it is <code>{escape(str(folder))}</code> '
                 '(<code>captcher extension</code> prints it again).')
    return templates.render("extension.html", **_chrome("extension", owner),
                            EXTENSION_DIR=where)


def account_page(owner):
    """The Settings page: appearance, connected browsers, the AI key."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        ai = ('<p class="panel-sub">Authoring and AI edits use your Anthropic API key, '
              'so they are billed to your Anthropic account.</p>')
    else:
        from . import envfile
        line = envfile.commented_out("ANTHROPIC_API_KEY")
        if line:
            fix = (f'Line {line} of <code>{escape(str(envfile.env_file()))}</code> has '
                   'your key but starts with <code>#</code>, so it is ignored. Delete '
                   'the <code>#</code> at the start of that line and restart Captcher.')
        else:
            fix = ('Add the line <code>ANTHROPIC_API_KEY=sk-ant-…</code> to '
                   f'<code>{escape(str(envfile.env_file()))}</code> and restart Captcher.')
        ai = ('<div class="notice notice--error">ANTHROPIC_API_KEY is not set, so AI '
              f'authoring and AI edits are unavailable. {fix}</div>')
    return templates.render("account.html", **_chrome("account", owner),
                            DEVICES=device_rows(owner), AI_STATUS=ai)


def settings_json(member_id):
    """GET /api/settings: what the catalog needs about this server and member."""
    return {
        "ai_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "verify_available": _verify_available(),
    }


def _verify_available():
    """Whether the render and replay checks can run here (the [verify] extra
    and its Chromium)."""
    from . import install
    return install.verify_status() == "ready"


USAGE_PERIODS = (("7", 7, "7 days"), ("30", 30, "30 days"),
                 ("90", 90, "90 days"), ("all", None, "All time"))
USAGE_DEFAULT_PERIOD = "30"
# Rows shown in the walkthrough and member tables; the rest are summed into
# one "N more" line so the totals still add up.
USAGE_TABLE_ROWS = 50


def _usd(value):
    """Dollars for the spend page: cents, except that a real cost under a cent
    says so instead of rounding to $0.00."""
    if value is None:
        return "—"
    if 0 < value < 0.005:
        return "&lt;$0.01"
    return f"${value:,.2f}"


def _tokens_short(n):
    """1,234,567 → 1.2M; the exact figure is in the title attribute."""
    n = int(n or 0)
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f'<span title="{n:,}">{n / size:.1f}{suffix}</span>'
    return f"{n:,}"


def _usage_rows(groups, label_cell, columns, empty):
    """Table rows for one breakdown. `columns` picks which numeric cells after
    the label: "runs", "calls", "per_run", "tokens", "cost"."""
    if not groups:
        return (f'<tr><td colspan="9" class="small muted usage-empty">'
                f'{escape(empty)}</td></tr>')
    shown, rest = groups[:USAGE_TABLE_ROWS], groups[USAGE_TABLE_ROWS:]
    rows = []
    for g in shown:
        cells = [f"<td>{label_cell(g)}</td>"]
        if "runs" in columns:
            cells.append(f'<td class="num">{g.get("jobs") or "—"}</td>')
        if "calls" in columns:
            cells.append(f'<td class="num">{g["calls"]:,}</td>')
        if "per_run" in columns:
            cells.append(f'<td class="num">{_usd(g.get("per_job"))}</td>')
        if "tokens" in columns:
            for f in ("input_tokens", "cache_read_tokens"):
                cells.append(f'<td class="num wide">{_tokens_short(g[f])}</td>')
            written = g["cache_write_5m_tokens"] + g["cache_write_1h_tokens"]
            cells.append(f'<td class="num wide">{_tokens_short(written)}</td>')
            cells.append(f'<td class="num wide">{_tokens_short(g["output_tokens"])}</td>')
        cost = _usd(g["cost"])
        if g.get("unpriced"):
            cost += (f' <span class="small muted" title="{g["unpriced"]} call(s) on a '
                     f'model with no price in usage.PRICES">+ unpriced</span>')
        cells.append(f'<td class="num">{cost}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    if rest:
        span = len(columns) + (3 if "tokens" in columns else 0)
        rows.append(
            f'<tr><td class="small muted">{len(rest)} more</td>'
            f'<td colspan="{span - 1}"></td>'
            f'<td class="num">{_usd(sum(g["cost"] for g in rest))}</td></tr>')
    return "".join(rows)


def usage_page(viewer=None, period=USAGE_DEFAULT_PERIOD):
    periods = {key: days for key, days, _ in USAGE_PERIODS}
    if period not in periods:
        period = USAGE_DEFAULT_PERIOD
    data = usage.report(periods[period])
    total = data["total"]
    ops = {o["key"]: o for o in data["by_operation"]}

    def models(g):
        return (f'<span class="small muted usage-models">'
                f'{escape(", ".join(g["models"]))}</span>')

    def operation_cell(g):
        return (escape(usage.OPERATION_LABELS.get(g["key"], g["key"]))
                + models(g))

    def walkthrough_cell(g):
        if not g["key"]:
            return '<span class="muted">No walkthrough (brand import, CLI)</span>'
        name = escape(g["label"] or "Untitled")
        if g.get("deleted"):
            return f'{name} <span class="pill pill--draft">Deleted</span>'
        return f'<a href="/player/{quote(g["key"])}?edit=1">{name}</a>'

    period_links = []
    for key, _, label in USAGE_PERIODS:
        current = ' is-active" aria-current="page' if key == period else ""
        period_links.append(f'<a class="chip{current}" '
                            f'href="/usage?days={key}">{label}</a>')
    notice = ""
    if total["unpriced"]:
        notice = (f'<div class="notice notice--error">{total["unpriced"]} call(s) ran on a '
                  'model with no price in <code>usage.PRICES</code>, so the totals below '
                  'leave them out. Add the model\'s price to count them.</div>')

    def per(key):
        return _usd(ops[key]["per_job"]) if ops.get(key) else "—"

    return templates.render(
        "usage.html", **_chrome("usage", viewer),
        PRICES_AS_OF=usage.PRICES_AS_OF,
        PERIODS="".join(period_links), NOTICE=notice,
        TOTAL_COST=_usd(total["cost"]), TOTAL_CALLS=f'{total["calls"]:,}',
        PER_AUTHOR=per("author"), PER_EDIT=per("ai-edit"),
        BY_OPERATION=_usage_rows(
            data["by_operation"], operation_cell,
            ("runs", "calls", "per_run", "tokens", "cost"),
            "No AI calls in this period."),
        BY_WALKTHROUGH=_usage_rows(
            data["by_walkthrough"], walkthrough_cell, ("runs", "calls", "cost"),
            "No AI calls in this period."),
        BY_MODEL=_usage_rows(
            data["by_model"], lambda g: f'<span class="mono">{escape(g["key"])}</span>',
            ("calls", "tokens", "cost"), "No AI calls in this period."),
        BY_DAY=_usage_rows(
            data["by_day"], lambda g: f'<span class="mono">{escape(g["key"])}</span>',
            ("calls", "cost"), "No AI calls in this period."))


def catalog_page(walkthroughs, owner=None):
    cards = '<div id="cards"><p class="empty">Loading…</p></div>'
    return templates.render(
        "catalog.html", **_chrome("catalog", owner),
        CARDS=cards,
        EXTENSION_PANEL=templates.load("_extension-panel.html"),
        CATALOG_SCRIPT=templates.load("_catalog-script.html"),
    )


# ---- capture review: every captured page + the actions recorded on it -----
# Read-only inspection of the raw capture behind a draft, so an author can
# see what the AI pass will work from before running it. Everything comes
# from routes that already exist: the capture manifest at
# /capture/<id>/walkthrough.json and the pages at /capture/<id>/<step>/…
# (served frozen by bridge.js).
def review_page(walkthrough_id, owner=None):
    from . import install
    checks = {"ready": _verify_available(), "help": install.VERIFY_HELP}
    return templates.render("review.html", **_chrome("catalog", owner),
                            ID=templates.script_json(walkthrough_id),
                            CHECKS=templates.script_json(checks))


def player_page():
    """The default walkthrough player (popup instructions — static/player2.*).
    No shared app toolbar here — the walkthrough starts immediately in its
    own focused chrome."""
    return (STATIC_DIR / "player2.html").read_text(encoding="utf-8")


def classic_player_page():
    """The classic side-panel player (static/player.*). Kept as a backup at
    /player1/ and still the edit surface (/player/<id>?edit=1) — the popup
    player is playback-only. Exports also build on these files."""
    return (STATIC_DIR / "player.html").read_text(encoding="utf-8")


def branding_page(owner=None):
    rows = ""
    for key, label, help_ in branding.BRANDING_COLORS:
        rows += (
            '<div class="color-row">'
            f'<div class="cr-info"><span class="cr-label">{escape(label)}</span>'
            f'<span class="cr-help">{escape(help_)}</span></div>'
            '<div class="cr-input">'
            f'<input type="color" data-color="{key}" class="cr-swatch" aria-label="{escape(label)} colour">'
            f'<input type="text" data-hex="{key}" class="cr-hex input input--mono" '
            f'maxlength="7" spellcheck="false" '
            f'aria-label="{escape(label)} hex value"></div></div>'
        )
    return templates.render("branding.html", **_chrome("branding", owner),
                            COLOR_ROWS=rows,
                            COLLECTION_JSON=templates.script_json(branding.branding_collection(owner)),
                            MYBRAND_JSON=templates.script_json(branding.my_brand_profile()))


def legacy_settings():
    """SHADOWCAPTURE_* names still set, sorted. Every setting is CAPTCHER_* now
    and the old names are not read, so a carried-over .env would otherwise be
    ignored without a word."""
    return sorted(k for k in os.environ if k.startswith("SHADOWCAPTURE_"))


def _already_captcher(port):
    """True when the process holding `port` on this machine is a Captcher."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/extension/ping",
                                    timeout=2) as r:
            return json.loads(r.read() or b"{}").get("captcher") is True
    except Exception:
        return False


def _open_browser(url):
    """Open `url` once per `captcher` run, never from a --dev reload, and
    never let a missing browser stop the server."""
    if os.environ.get("CAPTCHER_NO_BROWSER"):
        return
    os.environ["CAPTCHER_NO_BROWSER"] = "1"
    import webbrowser

    def go():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Timer(0.5, go).start()


def main(port=None, open_browser=False):
    """Start the server. `captcher` (cli.py) passes the port and asks for a
    browser tab; `python -m captcher.server [PORT]` serves without one."""
    if port is None:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = _env("CAPTCHER_HOST", "127.0.0.1")
    # With no accounts, whoever can reach the server owns everything on it.
    # Listening beyond this machine therefore needs an access token.
    if host not in LOOPBACK_HOSTS and not auth.ACCESS_TOKEN:
        sys.exit(f"Refusing to listen on {host}: anyone who can reach it would "
                 "control this server. Set CAPTCHER_ACCESS_TOKEN to a long random "
                 "string (people enter it once at /unlock), or bind to 127.0.0.1.")
    # What to open in the browser. A wildcard bind is still reached locally.
    local = host in LOOPBACK_HOSTS or host in ("0.0.0.0", "::", "")
    shown = "127.0.0.1" if local else host
    base = f"http://{'[' + shown + ']' if ':' in shown else shown}:{port}"

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if getattr(e, "errno", None) not in (48, 98):   # EADDRINUSE: macOS, Linux
            raise
        if _already_captcher(port):
            print(f"Captcher is already running at {base}/")
            if open_browser:
                _open_browser(base + "/")
            return
        sys.exit(f"Port {port} is in use by another program. Start Captcher on "
                 f"another one: captcher --port {port + 1}")

    # The banner says where the data is, so nobody has to guess which folder
    # to back up. Neither check exits: a server that boots with a clear FAIL
    # line is easier to fix than one that refuses to start.
    db_ok, db_detail = db.health()
    print(f"  database:  {'ok' if db_ok else 'FAIL'} — {db_detail}")
    blob_ok, blob_detail = blobs.health()
    print(f"  captures:  {'ok' if blob_ok else 'FAIL'} — {blob_detail}")
    from . import envfile, install
    print(f"  settings:  {envfile.env_file()}")
    print(f"  extension: {install.extension_folder()}")
    total = 0
    if db_ok:
        try:
            auth.ensure_local_owner()
            total = db.value("select count(*) from walkthroughs", default=0)
            print(f"Captcher {_package_version()} — {total} walkthrough(s).")
        except db.Error as e:
            print(f"  schema:   FAIL — {e}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[note] No ANTHROPIC_API_KEY yet. Recording, playback and export work "
              "without it. " + envfile.missing_key_hint())
    legacy = legacy_settings()
    if legacy:
        print("[warn] ignoring " + ", ".join(legacy) + " — settings are now named "
              "CAPTCHER_*; rename them in your .env or environment")
    if DEBUG:
        watch_and_reload()
        print("[dev] developer mode — restarts when "
              + ", ".join(f.name for f in WATCHED_FILES) + " change")
    print(f"\nCaptcher is running at {base}/  (Ctrl+C to stop)")
    if auth.ACCESS_TOKEN:
        print("  access token required — unlock at /unlock")
    if open_browser:
        # A first run has nothing to show yet; the setup guide is the start.
        _open_browser(base + ("/getting-started" if not total else "/"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def _package_version():
    try:
        from importlib.metadata import version
        return version("captcher")
    except Exception:
        return ""


if __name__ == "__main__":
    main()
