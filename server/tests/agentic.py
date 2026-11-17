"""Scripting the model side of the authoring/edit tool loops.

Both passes are tool loops, so a test that wants to say "the model returns
this walkthrough" has to say it as a `submit_*` tool call rather than as a
string. These helpers make that one line, and they script at the
`author._send_request` seam — the same seam the loop's own tests use, and the
one place every model call funnels through — so no test needs a network, an
API key, or knowledge of the wire format.

`submitting_walkthrough` is the workhorse: the loop's happy path is one turn that calls
submit with a walkthrough that validates. Tests asserting on *prompt content*
read it back out of the recorded request params with `prompt_text` /
`content_blocks`, which is strictly more than the old single-shot stubs could
see (they got the user prompt, but never the tools or the cache markers).
"""
import contextlib
import copy
import os
from unittest import mock

from captcher import author

# The loop checks for a key before it builds a request, and these helpers
# patch *below* that check — so every scripted run supplies a fake one. No
# test has ever needed a real key; this just keeps the check where it belongs.
FAKE_ENV = {"ANTHROPIC_API_KEY": "sk-test"}


def tool_use(name, inp, tu_id="tu_1"):
    return {"type": "tool_use", "id": tu_id, "name": name, "input": inp}


def reply(*blocks, stop_reason="tool_use"):
    return {"content": list(blocks), "stop_reason": stop_reason,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def scripted_sender(turns, log):
    """Pop one turn per request; record every request's params.

    Each turn is a function(params) -> wire-shaped response dict, so a turn
    can read the prompt it was given before deciding what to answer (picking
    a ref out of the digests, for instance).
    """
    remaining = list(turns)

    def fake_send(params):
        # snapshot: the loop mutates its messages list in place after the
        # request, so a live reference would show the final state everywhere
        log.append(copy.deepcopy(params))
        if not remaining:
            raise AssertionError("model called more times than scripted")
        return remaining.pop(0)(params)
    return fake_send


@contextlib.contextmanager
def scripted(turns):
    """Run the body with the model scripted. Yields the request log.

        with agentic.scripted([...]) as log:
            author.run_pass(WT_ID)
        assert "INTERACTIVE ELEMENTS" in agentic.prompt_text(log[0])
    """
    log = []
    with mock.patch.dict(os.environ, FAKE_ENV), \
            mock.patch.object(author, "_send_request", scripted_sender(turns, log)):
        yield log


def submitting(build, tool="submit_walkthrough"):
    """One submission: call `tool` with `build(params)` as its input.

    `build` receives the request params, so a turn can read the prompt it was
    given before deciding what to answer.
    """
    last = {}

    def first(params):
        last["input"] = build(params)
        return reply(tool_use(tool, last["input"]))

    def resubmit(params):
        # A submission with no errors but with warnings is handed back once
        # (author.WARNINGS_REVIEW_NOTE). This is the model judging them
        # harmless and resubmitting unchanged; unused when nothing warned.
        return reply(tool_use(tool, last["input"], tu_id="tu_2"))

    return scripted([first, resubmit])


def submitting_walkthrough(build):
    """submitting() for run_pass: `build(params) -> walkthrough dict`."""
    return submitting(lambda p: {"walkthrough": build(p)}, "submit_walkthrough")


def submitting_edit(build):
    """submitting() for run_edit: `build(params) -> the submit_edit input`."""
    return submitting(build, "submit_edit")


def repeating(build, tool="submit_walkthrough", turns=None):
    """The same submission every turn, until the loop gives up. For the
    model-never-fixes-it case."""
    turns = author.MAX_LOOP_ITERATIONS if turns is None else turns
    return scripted([lambda params: reply(tool_use(tool, build(params)))] * turns)


# ---- reading a recorded request -------------------------------------------

def content_blocks(params):
    """The initial user message's content blocks."""
    return params["messages"][0]["content"]


def prompt_text(params):
    """Every text block of the initial user prompt, joined.

    The single-shot stubs used to receive exactly this as their `user`
    argument, so an assertion that used to read `captured["user"]` reads this
    instead. Joining rather than taking block 0 matters for run_pass, whose
    prompt is text/image interleaved.
    """
    return "\n\n".join(b["text"] for b in content_blocks(params)
                       if b.get("type") == "text")


def system_text(params):
    """The system prompt as sent (a cache-marked block list)."""
    system = params.get("system")
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system or [])


def tool_names(params):
    return [t["name"] for t in params.get("tools") or []]


def ref_matching(text, label):
    """The ref id of the inventory line whose label is `label`.

    Digest inventories are `eNN kind "Label" sel:…` lines, and every test that
    picks a target does this same scan.
    """
    return next(ln.split()[0] for ln in text.splitlines()
                if ln.strip().startswith("e") and f'"{label}"' in ln)
