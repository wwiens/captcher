#!/usr/bin/env python3
"""
Who a request belongs to, for a single-user, self-hosted Captcher.

There are no accounts and no passwords. Everything belongs to one LOCAL OWNER,
a fixed row in `members` that the server creates for itself
(`ensure_local_owner`). The `owner_id` columns and filters stay throughout the
data layer, so a multi-user mode remains possible later; today they all hold
LOCAL_OWNER_ID.

What this module still provides:
  - Per-device capture API tokens for the browser extension: one row per paired
    browser in `capture_tokens`, stored as a SHA-256 digest. Revocation is per
    device.
  - One-time pairing codes, held in memory, that let a Captcher page hand the
    extension a credential without anybody copying a token. See
    docs/pairing.md.
  - Signed cookies (HMAC-SHA256). Bound to loopback, the browser needs none:
    a request with no cookie is the local owner. With CAPTCHER_ACCESS_TOKEN set
    (required when the server is reachable from other machines), the unlock
    page trades the token for one of these cookies. Tests also mint them to act
    as a particular member.

CLI (also `python -m captcher.auth ...`): see USAGE.
"""
import base64
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from . import db

ROOT = Path(__file__).resolve().parent
# In the data folder beside .env (config.py), so an installed package and a
# source checkout keep sessions the same way. Fixed at import, like the key.
SECRET_FILE = config.home() / "session_secret"

SESSION_COOKIE = "captcher_session"
SESSION_TTL = 30 * 24 * 60 * 60  # 30 days, in seconds

# The one owner of everything on a self-hosted server. A fixed id rather than a
# generated one, so a database can be moved between machines and still match.
LOCAL_OWNER_ID = "00000000-0000-4000-8000-000000000001"
# What the extension shows as the connected account (`/api/whoami` "email").
LOCAL_OWNER_LABEL = "you@localhost"

# Required to reach the server at all when it is exposed beyond loopback.
ACCESS_TOKEN = (os.environ.get("CAPTCHER_ACCESS_TOKEN") or "").strip()

# verify_session runs on every cookie-carrying request, including static assets,
# and it has to confirm the member still exists.
# Against a local file that was free; against Postgres it is a round trip per
# request. So member rows are cached for a few seconds.
#
# The window is what deleting a member row costs: it takes effect within
# MEMBER_CACHE_TTL rather than instantly.
MEMBER_CACHE_TTL = 5.0
_member_cache = {}            # member id -> (expiry epoch, row or None)
_member_cache_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------

def forget_member(member_id=None) -> None:
    """Drop cached member rows."""
    with _member_cache_lock:
        if member_id is None:
            _member_cache.clear()
        else:
            _member_cache.pop(str(member_id), None)


def get_member(member_id):
    """One member row by id, or None. Cached for MEMBER_CACHE_TTL.

    A miss is cached too. Otherwise a stale cookie for a deleted account would
    hit the database on every request it makes, which is precisely the traffic
    least worth serving.
    """
    if not member_id:
        return None
    key = str(member_id)
    now = time.monotonic()
    with _member_cache_lock:
        hit = _member_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    row = db.one(
        """select id, email, created_at, active_brand_profile_id
             from members where id = %s""",
        (member_id,),
    )
    with _member_cache_lock:
        _member_cache[key] = (now + MEMBER_CACHE_TTL, row)
    return row


def member_exists(member_id) -> bool:
    """Whether a member row exists — the one check sessions and capture tokens
    make before they are honoured."""
    return get_member(member_id) is not None


def ensure_local_owner() -> str:
    """Create the local owner's row if it is missing; return its id.

    Cheap when the row exists (a cached read). The row is what every owner_id
    foreign key points at, so it must exist before the first walkthrough.
    """
    if get_member(LOCAL_OWNER_ID) is None:
        db.execute(
            """insert into members (id, email) values (%s, %s)
               on conflict (id) do nothing""",
            (LOCAL_OWNER_ID, LOCAL_OWNER_LABEL),
        )
        forget_member(LOCAL_OWNER_ID)
    return LOCAL_OWNER_ID


def access_token_ok(value: str) -> bool:
    """Whether `value` is CAPTCHER_ACCESS_TOKEN. Constant-time; False when no
    token is configured, so an empty form never unlocks anything."""
    if not ACCESS_TOKEN or not value:
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"),
                               ACCESS_TOKEN.encode("utf-8"))


# --------------------------------------------------------------------------
# capture API tokens (one per paired browser; used by the capture extension)
# --------------------------------------------------------------------------
# A member holds a LIST of tokens, one per browser they have paired. It was once
# one token per member, and minting replaced it: pairing a second browser
# silently revoked the first, with no error anywhere — the older extension just
# started getting 401s, whose message sent its owner to regenerate, breaking the
# other browser in turn.
#
# The prefix is the discriminator `find_member_by_token` rejects non-tokens on.
# It is short for Captcher. Every issued token carries it, so changing it 401s
# every paired extension at once: pick it once and leave it.
API_TOKEN_PREFIX = "cap_"
MAX_LABEL = 64

# `last_used` is a diagnostic ("which of these three is my laptop?"), not an
# audit record. Uploads are the hot path, so it is written at most this often
# per token.
_TOUCH_INTERVAL = 3600  # seconds
_touched = {}           # token digest -> monotonic time of last write
_touch_lock = threading.Lock()


def _hash_api_token(token: str) -> str:
    # High-entropy random tokens do not need a slow KDF — a plain SHA-256 digest
    # is enough to make a leaked capture_tokens table useless for uploads.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_label(label: str) -> str:
    """A device label is chosen by whoever redeemed the pairing code, so it is
    attacker-controlled in principle. Length is bounded here; escaping is the
    renderer's job."""
    return (label or "").strip()[:MAX_LABEL] or "Browser extension"


def generate_api_token(member_id, label: str = ""):
    """Add a capture token for a member's new browser.

    Returns `(plaintext_token, device_id)` — the plaintext is returned once and
    never stored — or `(None, None)` when the member does not exist. Replaces
    nothing: existing devices keep working.
    """
    if not member_exists(member_id):
        return None, None
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    device_id = secrets.token_urlsafe(9)
    try:
        db.execute(
            """insert into capture_tokens (id, member_id, token_hash, label)
               values (%s, %s, %s, %s)""",
            (device_id, member_id, _hash_api_token(token), _clean_label(label)),
        )
    except Exception:
        return None, None
    return token, device_id


def find_member_by_token(token: str):
    """The member id owning a capture token, else None.

    An indexed probe on the digest, where the filesystem mirror forced a scan of
    every account's every device. There is no constant-time comparison here and
    none is needed: the lookup key is a SHA-256 of 32 random bytes, so there is
    no low-entropy secret for a timing signal to leak.
    """
    if not token or not token.startswith(API_TOKEN_PREFIX):
        return None
    digest = _hash_api_token(token)
    row = db.one(
        "select id, member_id from capture_tokens "
        "where token_hash = %s", (digest,)
    )
    if not row:
        return None
    member_id = row["member_id"]
    # A deleted member's token stops working on its next use.
    if not member_exists(member_id):
        return None
    _touch_token(row["id"], digest)
    return member_id


def _touch_token(device_id: str, digest: str) -> None:
    """Record that a device was used, at most once per _TOUCH_INTERVAL.

    Rate-limited in memory rather than by reading the stored timestamp first,
    which would double the queries on the upload path to maintain a diagnostic.
    Best-effort by construction: this sits on the upload path, and a timestamp
    that cannot be written is not a reason to fail an upload.
    """
    now = time.monotonic()
    with _touch_lock:
        last = _touched.get(digest)
        if last and (now - last) < _TOUCH_INTERVAL:
            return
        _touched[digest] = now
        # The map is bounded by the number of paired browsers, but a long-lived
        # process seeing many revoked devices would still grow it slowly.
        if len(_touched) > 4096:
            cutoff = now - _TOUCH_INTERVAL
            for key in [k for k, v in _touched.items() if v < cutoff]:
                _touched.pop(key, None)
    try:
        db.execute(
            "update capture_tokens set last_used_at = now() where id = %s", (device_id,)
        )
    except Exception:
        pass


def list_devices(member_id) -> list:
    """Every paired browser for a member, newest first, without any digest.

    What `/account` renders and what the revoke button names. The digest is
    dropped by the SELECT rather than by the caller, so no template can print
    it by accident.
    """
    return db.query(
        """select id, label, created_at, last_used_at
             from capture_tokens
            where member_id = %s
         order by created_at desc""",
        (member_id,),
    )


def revoke_device(member_id, device_id: str) -> bool:
    """Drop one device token. True if something was removed.

    Filtered on member_id as well as the device id, so knowing another member's
    device id is not enough to revoke it.
    """
    if not device_id:
        return False
    return db.execute(
        "delete from capture_tokens where id = %s and member_id = %s",
        (device_id, member_id),
    ) > 0


# --------------------------------------------------------------------------
# pairing codes (one-time, short-lived; see PAIRING.md)
# --------------------------------------------------------------------------
# The code is what a signed-in page hands the extension so the extension can
# fetch a real token for itself. It is deliberately:
#
#   - in memory only. A restart between offer and claim costs one re-click; the
#     alternative is a redeemable credential surviving a crash on disk.
#   - single use, consumed under a lock BEFORE the token is minted, so two
#     simultaneous claims cannot both succeed.
#   - short lived. 120s is the width of "click the button, watch it work".
#
# It travels over window.postMessage and is therefore visible to the page and to
# anything running in it. That is the whole reason it is not itself a token: the
# worst an intercepted code can do is pair an extension the interceptor
# controls, which shows up on /account as a device row the member can revoke.
PAIRING_CODE_PREFIX = "cap_pair_"
PAIRING_CODE_TTL = 120  # seconds

_PAIRING_CODES = {}          # code -> (member id, expiry epoch)
_PAIRING_LOCK = threading.Lock()


def _purge_pairing_codes(now: float) -> None:
    """Drop expired codes. Called under _PAIRING_LOCK from both sides, so the
    map cannot grow without bound even if nobody ever claims."""
    for code in [c for c, (_, exp) in _PAIRING_CODES.items() if exp <= now]:
        _PAIRING_CODES.pop(code, None)


def issue_pairing_code(member_id):
    """Mint a one-time pairing code for the member behind a browser page.

    Returns `(code, ttl_seconds)`. Callers must have come from a browser page,
    not a capture token — a capture token must not be able to mint credentials
    for itself.
    """
    code = PAIRING_CODE_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    with _PAIRING_LOCK:
        _purge_pairing_codes(now)
        _PAIRING_CODES[code] = (str(member_id), now + PAIRING_CODE_TTL)
    return code, PAIRING_CODE_TTL


def redeem_pairing_code(code: str, label: str = ""):
    """Consume a pairing code and mint a device token for its owner.

    Returns `(member_id, token, device_id)`, or `(None, None, None)` for a code
    that is expired, already used, or was never issued. The three are
    deliberately indistinguishable: telling them apart tells a guesser which
    half of the space to search, and no legitimate user behaves differently in
    the three cases.
    """
    if not code or not code.startswith(PAIRING_CODE_PREFIX):
        return None, None, None
    now = time.time()
    with _PAIRING_LOCK:
        _purge_pairing_codes(now)
        # Pop before minting: the code is spent even if the mint below fails,
        # which is the safe direction — a failed pair is one more click, a
        # replayable code is a credential.
        entry = _PAIRING_CODES.pop(code, None)
    if not entry:
        return None, None, None
    member_id, expiry = entry
    if expiry <= now:
        return None, None, None
    token, device_id = generate_api_token(member_id, label)
    if not token:
        return None, None, None
    return member_id, token, device_id


# --------------------------------------------------------------------------
# session secret + signed tokens
# --------------------------------------------------------------------------
def _load_secret() -> bytes:
    """Session-signing key. From CAPTCHER_SESSION_SECRET, else a persisted
    random file, so sessions survive restarts without configuration."""
    env = (os.environ.get("CAPTCHER_SESSION_SECRET") or "").strip()
    if env:
        return env.encode("utf-8")
    if SECRET_FILE.is_file():
        try:
            data = SECRET_FILE.read_text(encoding="utf-8").strip()
            if data:
                return data.encode("utf-8")
        except OSError:
            pass
    secret = secrets.token_urlsafe(48)
    try:
        SECRET_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Created 0600 from the first byte rather than chmod'd after: a
        # write-then-chmod leaves the signing key world-readable for the gap
        # between the two calls under a default umask.
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret + "\n")
    except OSError as e:
        # Not fatal, but the key is now per-process: every restart invalidates
        # every session. Say so rather than failing silently.
        print(f"[auth] WARNING: could not persist {SECRET_FILE.name} ({e}). "
              "Using an ephemeral key — sessions will not survive a restart.")
    return secret.encode("utf-8")


_SECRET = _load_secret()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_session(member_id, ttl: int = SESSION_TTL) -> str:
    """A signed token: <payload>.<sig>, payload = b64(member_id|expiry).

    The payload carries the uuid, not the address. A member who changes their
    email keeps their session, because the session never named the address.
    """
    expiry = int(time.time()) + ttl
    payload = f"{member_id}|{expiry}".encode()
    body = _b64u(payload)
    sig = _b64u(hmac.new(_SECRET, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_session(token: str):
    """The member id for a valid, unexpired token, else None.

    The signature is checked before anything is decoded, and the existence check
    runs last: a validly signed cookie for a member who has since been deleted
    is not a session.
    """
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64u(hmac.new(_SECRET, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        member_id, _, expiry = _b64u_decode(body).decode("utf-8").partition("|")
        if int(expiry) < int(time.time()):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return member_id if member_exists(member_id) else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
USAGE = """\
captcher token [LABEL]     add a capture token, for scripted uploads
captcher devices           list paired browsers and tokens
captcher revoke ID         revoke one"""


def cli(args=None):
    """`captcher token|devices|revoke`, and `python -m captcher.auth`."""
    args = list(sys.argv[1:] if args is None else args)
    cmd = args[0] if args else ""
    owner = ensure_local_owner()

    if cmd == "token":
        label = " ".join(args[1:]).strip()[:MAX_LABEL] or "CLI"
        token, device_id = generate_api_token(owner, label)
        if not token:
            sys.exit("Could not create a capture token — is the database reachable?")
        print(f"Capture token (device {device_id}; existing devices keep working):")
        print(f"  {token}")
        print("Send it as `Authorization: Bearer <token>`. Revoke it with "
              f"`captcher revoke {device_id}`.")
        return 0

    if cmd == "devices":
        devices = list_devices(owner)
        if not devices:
            print("No paired devices.")
            return 0
        for d in devices:
            used = (f"last used {d['last_used_at']:%Y-%m-%d}" if d["last_used_at"]
                    else "never used")
            print(f"  {d['id']}  {d['label']}  (added {d['created_at']:%Y-%m-%d}, {used})")
        return 0

    if cmd == "revoke" and len(args) >= 2:
        if revoke_device(owner, args[1]):
            print("Revoked.")
            return 0
        print("No such device. `captcher devices` lists them.")
        return 1

    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(cli())
