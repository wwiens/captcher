"""Pytest configuration.

Puts the repo root on sys.path so `import server` / `import export` resolve
regardless of how pytest is invoked. Previously this worked only by accident:
pytest inserts the rootdir when there is no __init__.py, so adding one to
tests/ (needed for `from . import fixtures`) would otherwise have broken
collection.

Then it gives the suite a real, throwaway data folder — a SQLite file and a
captures folder in a temporary CAPTCHER_HOME — emptied before every test.
"""
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Nothing leaves this machine
# ---------------------------------------------------------------------------
# The suite stubs urlopen to make the Claude calls fail in specific ways, and
# with tracing on those stubbed failures were posted to the developer's real
# LangSmith project as genuine-looking "ai edit" and "brand inference" errors.
# Same class of mistake as running a mutating request against the real data
# dir: tests must not touch live services.
#
# Setting the vars empty rather than deleting them is what makes this stick —
# envfile.load_env() fills in from .env only for names *absent* from the
# environment, so an empty value shadows the .env line. Must happen before any
# app module is imported, which conftest import order guarantees.
#
# tests/test_langsmith_trace.py opts itself back in per-test and stubs the
# transport, so nothing leaves the machine there either.
#
# ANTHROPIC_API_KEY is blanked for the same reason again: the preflight's
# count_tokens rescue is a real API call, and a developer holding a real .env
# would otherwise fire it from the over-budget preflight tests. Tests that
# exercise the request path set their own "sk-test" via mock.patch.dict.
#
# The last three are a different concern and are grouped separately for that
# reason: nothing leaks if they are set, but server.py and auth.py read them
# into module constants at import, and the suite assumes the unconfigured
# behaviour (a cookie-less browser is the local owner; /examples/<slug>
# answering 404 for everything). A developer whose .env carries a deployment's
# values would see those fail with no hint that the cause was their own
# configuration. Tests that want access-token mode patch auth.ACCESS_TOKEN.
for _var in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY",
             "LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY",
             "ANTHROPIC_API_KEY",
             "CAPTCHER_ACCESS_TOKEN", "CAPTCHER_ALLOWED_HOSTS",
             "CAPTCHER_EXAMPLE_WALKTHROUGHS"):
    os.environ[_var] = ""


# ---------------------------------------------------------------------------
# A real, throwaway data folder
# ---------------------------------------------------------------------------
# The stores are SQL and the captures are files, so the tests use the real
# thing: one SQLite file and one captures folder in a temporary CAPTCHER_HOME
# made for this session, never the developer's ~/.captcher. Set here, at
# import, before any captcher module reads it, and overriding whatever the
# shell or .env said. Every test starts with empty tables and an empty
# captures folder (see the autouse fixture below).
_TEST_HOME = Path(tempfile.mkdtemp(prefix="captcher-test-"))
os.environ["CAPTCHER_HOME"] = str(_TEST_HOME)
os.environ.pop("CAPTCHER_DB", None)

def test_home():
    """This session's CAPTCHER_HOME."""
    return _TEST_HOME


# ---------------------------------------------------------------------------
# Making members
# ---------------------------------------------------------------------------

def new_member(email=None):
    """Mint a member and return its id (a uuid string).

    Single-user Captcher has one real member, the local owner. Tests mint more
    so ownership filters stay tested: every owner_id column is still there.
    The address is per-call and under `.invalid`, which RFC 2606 reserves.
    """
    from captcher import auth
    from captcher import db

    member_id = str(uuid.uuid4())
    email = email or f"member-{member_id}@captcher.invalid"
    db.execute("insert into members (id, email) values (%s, %s)", (member_id, email))
    auth.forget_member(member_id)
    return member_id


def signed_in_client(base_url, member_id):
    """A cookie-jar opener already carrying a valid session for `member_id`.

    The cookie only this server can mint (the unlock page mints the same one).
    Without it a request is the local owner; with it, the named member — which
    is how the suites keep testing that ownership filters hold.
    """
    import http.cookiejar
    import urllib.request

    from captcher import auth

    host = urllib.parse.urlsplit(base_url).hostname or "127.0.0.1"
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name=auth.SESSION_COOKIE, value=auth.make_session(member_id),
        port=None, port_specified=False,
        domain=host, domain_specified=False, domain_initial_dot=False,
        path="/", path_specified=True,
        secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------

def pytest_sessionfinish(session, exitstatus):
    from captcher import db

    db.reset()
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


# How long to wait for one leftover job thread. A thread still parked on a
# test's gate after this is abandoned; it writes nothing until released.
JOB_THREAD_JOIN_SECONDS = 5


def _join_job_threads():
    """Join the threads server.run_as_job starts (named "<kind>:<walkthrough>")."""
    server = sys.modules.get("captcher.server")
    if server is None:
        return
    prefixes = tuple(f"{kind}:" for kind in server.JOB_LABELS)
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.name.startswith(prefixes):
            thread.join(JOB_THREAD_JOIN_SECONDS)


@pytest.fixture(autouse=True)
def _empty_data():
    """Empty tables and an empty captures folder for every test.

    Emptying before rather than after means a failing test's rows and files
    survive it, which is worth having when you are looking at one.

    A background job outlives the test that started it: tearDowns mark jobs
    finished, but the thread still writes its AI log afterwards. A write
    landing after the wipe would leak into the next test, so the previous
    tests' job threads are joined first.
    """
    from captcher import auth
    from captcher import config
    from captcher import db

    _join_job_threads()
    # Deleting members cascades through walkthroughs (and so history, logs and
    # collection items), collections, brand profiles and capture tokens.
    # ai_usage has no foreign keys on purpose: spend outlives what it was
    # spent on.
    db.execute("delete from ai_usage")
    db.execute("delete from members")
    shutil.rmtree(config.captures_dir(), ignore_errors=True)
    auth.forget_member()
    yield
