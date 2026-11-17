"""What the Claude API calls cost, and what they were for.

Every Messages API response reports its own token usage, and `author._request`
hands each one to `record()`, which writes one `ai_usage` row per billed
attempt. Attribution — which operation, walkthrough, member and job a call was
made for — does not travel through the call chain. It is set once, where the
work starts, with `attribute()`, and read back from a context variable when the
call returns. That keeps every pass, check and prompt builder free of a
parameter that only bookkeeping needs.

WHERE ATTRIBUTION IS SET

  server.run_as_job          author, ai-edit, render-check, verify-replay
  server.handle_ai_edit      relabels an edit that only answered as ai-assist
  server.handle_branding_from_url   brand-import

A call made with nothing set (a CLI run, a script) is recorded as `other`.

A context variable does not cross into a thread pool by itself: a worker
thread starts with an empty context. `render_check` judges pages in a pool and
runs each judgement inside `contextvars.copy_context()` taken in the parent for
exactly this reason; any new pool around a model call needs the same.

WHY ATTEMPTS, NOT CALLS

A request the primary model declines can be re-served by the server-side
fallback model inside the same call. The response's `usage.iterations` is then
the per-attempt record, each attempt billed at the price of the model that ran
it, and the top-level `usage` covers only the attempt that produced the reply.
So a rescued call is two rows sharing a `call_id`.

PRICES

`PRICES` is the list price in USD per million tokens, from
https://platform.claude.com/docs/en/about-claude/pricing (read 2026-10-06).
Costs are computed when a report is read, never stored, so correcting a price
re-prices history. They are estimates: no fast mode, US-only inference or batch
discount is applied, because the server uses none of them, and negotiated
discounts are invisible here. The Anthropic Console is the bill.

Recording is best-effort by construction. A row that could not be written must
never fail the paid call it describes.
"""
import contextlib
import contextvars
import uuid
from datetime import datetime, timedelta, timezone

from . import db

# USD per million tokens:
#   (input, 5-minute cache write, 1-hour cache write, cache read, output)
# Matched by exact id or a dated snapshot of it (`<id>-<date>`), longest id
# first, so claude-opus-5-5 never prices as claude-opus-5.
PRICES = {
    "claude-fable-5-1":  (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-mythos-5-1": (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-fable-5":    (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-mythos-5":   (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-opus-5-5":   (4.00, 5.00, 8.00, 0.20, 20.00),
    "claude-opus-5":     (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-8":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-7":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-6":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-5":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-sonnet-5-5": (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-sonnet-5":   (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-haiku-4-5":  (1.00, 1.25, 2.00, 0.10, 5.00),
}
PRICES_AS_OF = "2026-10-06"

# The job kinds server.run_as_job claims, plus the two set elsewhere. Order is
# the order the admin page lists them in.
OPERATION_LABELS = {
    "author": "Authoring pass",
    "ai-edit": "AI edit",
    "ai-assist": "AI assist (answered)",
    "render-check": "Render check",
    "verify-replay": "Replay check",
    "brand-import": "Brand import",
    "other": "Other (CLI, scripts)",
}

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_5m_tokens", "cache_write_1h_tokens")

_attribution = contextvars.ContextVar("ai_usage_attribution", default=None)


@contextlib.contextmanager
def attribute(operation, *, walkthrough_id=None, member_id=None, job_id=None):
    """Attribute every model call made inside the block (in this thread, or in
    a context copied from it) to one operation, walkthrough, member and job."""
    token = _attribution.set({"operation": operation,
                              "walkthrough_id": walkthrough_id,
                              "member_id": member_id,
                              "job_id": job_id})
    try:
        yield
    finally:
        _attribution.reset(token)


def current():
    """The attribution in effect, or None."""
    return _attribution.get()


def price_for(model):
    """The PRICES row for a model id or a dated snapshot of one, else None."""
    model = (model or "").removeprefix("anthropic.")
    for name in sorted(PRICES, key=len, reverse=True):
        if model == name or model.startswith(name + "-"):
            return PRICES[name]
    return None


def cost(model, tokens):
    """USD for one model's token counts (a dict keyed by TOKEN_FIELDS), or
    None for a model with no price."""
    p = price_for(model)
    if p is None:
        return None
    per_mtok = (tokens.get("input_tokens", 0) * p[0]
                + tokens.get("cache_write_5m_tokens", 0) * p[1]
                + tokens.get("cache_write_1h_tokens", 0) * p[2]
                + tokens.get("cache_read_tokens", 0) * p[3]
                + tokens.get("output_tokens", 0) * p[4])
    return per_mtok / 1_000_000


def _int(value):
    return value if isinstance(value, int) and value > 0 else 0


def _tokens(usage):
    """One usage object (a whole response's, or one iteration's) as our token
    fields. A cache write is 5-minute unless the breakdown says otherwise."""
    five_min = _int(usage.get("cache_creation_input_tokens"))
    one_hour = 0
    split = usage.get("cache_creation")
    if isinstance(split, dict) and ("ephemeral_5m_input_tokens" in split
                                    or "ephemeral_1h_input_tokens" in split):
        five_min = _int(split.get("ephemeral_5m_input_tokens"))
        one_hour = _int(split.get("ephemeral_1h_input_tokens"))
    return {"input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
            "cache_write_5m_tokens": five_min,
            "cache_write_1h_tokens": one_hour}


def attempts(data, requested_model):
    """The billed attempts in one response, as [(model, tokens)].

    `usage.iterations` is the per-attempt record when the fallback ran. An
    iteration that names no model is the primary (the requested model) unless
    it is the fallback's own, which is the model that served the reply."""
    usage = data.get("usage") or {}
    served = data.get("model") or requested_model or "unknown"
    out = []
    for it in usage.get("iterations") or []:
        if not isinstance(it, dict):
            continue
        tokens = _tokens(it)
        if not any(tokens.values()):
            continue
        model = it.get("model") or (served if it.get("type") == "fallback_message"
                                    else requested_model or served)
        out.append((model, tokens))
    return out or [(served, _tokens(usage))]


_warned = set()


def record(data, requested_model):
    """Store the attempts in one Messages API response. Never raises."""
    try:
        if not db.configured():
            return
        who = current() or {}
        walkthrough_id = who.get("walkthrough_id")
        title = None
        if walkthrough_id:
            # Kept on the row so the spend still has a name after the
            # walkthrough is deleted. Read once per attribution, not per call.
            if "walkthrough_title" not in who:
                who["walkthrough_title"] = db.value(
                    "select title from walkthroughs where id = %s",
                    (walkthrough_id,))
            title = who.get("walkthrough_title")
        call_id = uuid.uuid4()
        for n, (model, tokens) in enumerate(attempts(data, requested_model)):
            db.execute(
                """insert into ai_usage
                     (call_id, attempt, operation, job_id, walkthrough_id,
                      walkthrough_title, member_id, model, requested_model,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_write_5m_tokens, cache_write_1h_tokens,
                      stop_reason, message_id)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s)""",
                (call_id, n, who.get("operation") or "other", who.get("job_id"),
                 walkthrough_id, title, who.get("member_id"), model,
                 requested_model, *(tokens[f] for f in TOKEN_FIELDS),
                 data.get("stop_reason"), data.get("id")))
    except Exception as e:
        # One line per kind of failure, not one per call: a missing table
        # would otherwise print on every model request of every pass.
        kind = type(e).__name__
        if kind not in _warned:
            _warned.add(kind)
            print(f"[warn] AI usage not recorded ({kind}: {e}); "
                  f"further {kind} failures are silent")


def relabel(job_id, operation):
    """Move one job's calls to another operation. An AI edit that ended by
    answering is only known to be ai-assist once it has finished."""
    if not job_id:
        return
    try:
        db.execute("update ai_usage set operation = %s where job_id = %s",
                   (operation, job_id))
    except Exception as e:
        print(f"[warn] AI usage for job {job_id} not relabelled: {e}")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)) if days else None


def _rollup(key_sql, since):
    """Token sums per (key, model) since a time, priced into one row per key.

    `key_sql` is an expression over ai_usage chosen inside this module, never
    taken from a request."""
    rows = db.query(
        f"""select {key_sql} as key, model,
                   count(*) as attempts,
                   count(distinct call_id) as calls,
                   sum(input_tokens) as input_tokens,
                   sum(output_tokens) as output_tokens,
                   sum(cache_read_tokens) as cache_read_tokens,
                   sum(cache_write_5m_tokens) as cache_write_5m_tokens,
                   sum(cache_write_1h_tokens) as cache_write_1h_tokens
              from ai_usage
             where (%s is null or created_at >= %s)
          group by 1, 2""",
        (since, since),
    )
    out = {}
    for r in rows:
        agg = out.setdefault(r["key"], {
            "key": r["key"], "label": "", "calls": 0, "cost": 0.0,
            "unpriced": 0, "models": set(), **dict.fromkeys(TOKEN_FIELDS, 0)})
        tokens = {f: int(r[f] or 0) for f in TOKEN_FIELDS}
        for f in TOKEN_FIELDS:
            agg[f] += tokens[f]
        # A fallback-rescued call is counted under both of its models; that
        # double count is rare enough to leave in the per-key call total.
        agg["calls"] += r["calls"]
        agg["models"].add(r["model"])
        c = cost(r["model"], tokens)
        if c is None:
            agg["unpriced"] += r["attempts"]
        else:
            agg["cost"] += c
    return out


def _jobs_per(key_sql, since):
    """Distinct jobs per key — read without the model, so a job whose calls
    span two models is still one job."""
    return {r["key"]: r["jobs"] for r in db.query(
        f"""select {key_sql} as key, count(distinct job_id) as jobs
              from ai_usage
             where (%s is null or created_at >= %s)
          group by 1""", (since, since))}


WALKTHROUGH_KEY = "coalesce(walkthrough_id, '')"
MEMBER_KEY = "coalesce(member_id, '')"


def report(days=30):
    """Spend since `days` ago (all time for None), broken down five ways.
    Every list is sorted by cost, highest first, except `by_day` (newest
    first)."""
    since = _since(days)

    def ranked(key_sql, with_jobs=True):
        groups = _rollup(key_sql, since)
        jobs = _jobs_per(key_sql, since) if with_jobs else {}
        rows = []
        for g in groups.values():
            g["models"] = sorted(g["models"])
            g["jobs"] = jobs.get(g["key"], 0)
            g["per_job"] = g["cost"] / g["jobs"] if g["jobs"] else None
            rows.append(g)
        return sorted(rows, key=lambda g: g["cost"], reverse=True)

    by_operation = ranked("operation")
    by_walkthrough = ranked(WALKTHROUGH_KEY)
    # The live title where the walkthrough still exists, else the one it had.
    titles = {r["key"]: r for r in db.query(
        f"""select {WALKTHROUGH_KEY} as key,
                   coalesce(max(w.title), max(u.walkthrough_title), '') as title,
                   max(w.id is not null) as live
              from ai_usage u left join walkthroughs w on w.id = u.walkthrough_id
          group by 1""")}
    for w in by_walkthrough:
        t = titles.get(w["key"]) or {}
        w["label"] = t.get("title") or ""
        w["deleted"] = bool(w["key"]) and not t.get("live")
    by_member = ranked(MEMBER_KEY)
    emails = {r["id"]: r["email"] for r in db.query(
        "select id, email from members")}
    for m in by_member:
        m["label"] = emails.get(m["key"], "")
    by_model = ranked("model", with_jobs=False)
    by_day = ranked("substr(created_at, 1, 10)",
                    with_jobs=False)
    by_day.sort(key=lambda g: g["key"], reverse=True)

    total = {"cost": sum(o["cost"] for o in by_operation),
             "calls": sum(o["calls"] for o in by_operation),
             "jobs": sum(o["jobs"] for o in by_operation),
             "unpriced": sum(o["unpriced"] for o in by_operation),
             **{f: sum(o[f] for o in by_operation) for f in TOKEN_FIELDS}}
    return {"days": days, "since": since, "total": total,
            "by_operation": by_operation, "by_walkthrough": by_walkthrough,
            "by_member": by_member, "by_model": by_model, "by_day": by_day}
