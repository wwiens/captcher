"""The streaming accumulator under `author._send_request`.

`_send_request` folds the Messages API event stream into the wire-shaped dict
every caller in author.py reads with `.get`. It does that itself instead of
calling the SDK's `.stream(...).get_final_message()`, because that helper
reads events through typed models and dies on a well-formed response whenever
the API ships a field ahead of the installed SDK:

    An authoring pass failed unexpectedly: 'dict' object has no attribute
    'container'

The mechanism is in `_accumulate_stream`'s docstring. These tests pin the two
halves of the contract: the fold reproduces exactly what the SDK produced on a
response both agree on, and it survives the one that broke it.
"""
import json
import unittest

from captcher import author


class FakeEvent:
    """A stream event as the SDK hands it over — `.to_dict()` and nothing the
    accumulator is allowed to depend on."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return json.loads(json.dumps(self._payload))


def message_start(**usage):
    return FakeEvent({
        "type": "message_start",
        "message": {"id": "msg_1", "type": "message", "role": "assistant",
                    "model": "claude-opus-5", "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 120, "output_tokens": 1, **usage}},
    })


def block_start(index, block):
    return FakeEvent({"type": "content_block_start", "index": index,
                      "content_block": block})


def block_delta(index, delta):
    return FakeEvent({"type": "content_block_delta", "index": index,
                      "delta": delta})


def block_stop(index):
    return FakeEvent({"type": "content_block_stop", "index": index})


def message_delta(delta, usage):
    return FakeEvent({"type": "message_delta", "delta": delta, "usage": usage})


MESSAGE_STOP = FakeEvent({"type": "message_stop"})


class AccumulatorTests(unittest.TestCase):

    def test_folds_a_full_turn_into_the_wire_shape(self):
        """Thinking, text and a tool call, in the shape the loop sends back
        verbatim as the next assistant turn."""
        data = author._accumulate_stream([
            message_start(cache_read_input_tokens=80),
            block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
            block_delta(0, {"type": "thinking_delta", "thinking": "weigh "}),
            block_delta(0, {"type": "thinking_delta", "thinking": "the steps"}),
            block_delta(0, {"type": "signature_delta", "signature": "sig-abc"}),
            block_stop(0),
            block_start(1, {"type": "text", "text": ""}),
            block_delta(1, {"type": "text_delta", "text": "Reading "}),
            block_delta(1, {"type": "text_delta", "text": "step-004."}),
            block_stop(1),
            block_start(2, {"type": "tool_use", "id": "tu_1",
                            "name": "read_page_html", "input": {}}),
            block_delta(2, {"type": "input_json_delta",
                            "partial_json": '{"page":"step-'}),
            block_delta(2, {"type": "input_json_delta",
                            "partial_json": '004/page.html"}'}),
            block_stop(2),
            message_delta({"stop_reason": "tool_use", "stop_sequence": None},
                          {"output_tokens": 347}),
            MESSAGE_STOP,
        ])

        self.assertEqual(data["stop_reason"], "tool_use")
        self.assertEqual(data["content"], [
            {"type": "thinking", "thinking": "weigh the steps",
             "signature": "sig-abc"},
            {"type": "text", "text": "Reading step-004."},
            {"type": "tool_use", "id": "tu_1", "name": "read_page_html",
             "input": {"page": "step-004/page.html"}},
        ])
        # The delta reports output_tokens only; the start's input side and the
        # cache counters have to survive it, because every sizing decision and
        # the "is caching working" log line read them.
        self.assertEqual(data["usage"], {"input_tokens": 120,
                                         "output_tokens": 347,
                                         "cache_read_input_tokens": 80})

    def test_no_partial_json_key_leaks_to_callers(self):
        """The mid-stream buffer is an implementation detail; a caller sending
        the block back to the API must not carry it."""
        data = author._accumulate_stream([
            message_start(),
            block_start(0, {"type": "tool_use", "id": "tu_1",
                            "name": "validate_walkthrough", "input": {}}),
            block_delta(0, {"type": "input_json_delta", "partial_json": "{}"}),
            block_stop(0),
            message_delta({"stop_reason": "tool_use"}, {"output_tokens": 3}),
            MESSAGE_STOP,
        ])
        self.assertEqual(data["content"][0]["input"], {})
        for block in data["content"]:
            self.assertNotIn("_partial_json", block)

    def test_unknown_fields_and_events_ride_through(self):
        """The regression. A field the installed SDK does not model is carried
        into the result instead of failing the pass, and an event kind it has
        never seen is ignored rather than raising."""
        data = author._accumulate_stream([
            message_start(),
            FakeEvent({"type": "some_future_event", "payload": {"a": 1}}),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "ok"}),
            block_stop(0),
            message_delta(
                {"stop_reason": "end_turn", "stop_sequence": None,
                 "container": {"id": "c1", "expires_at": "2026-08-12T00:00:00Z"}},
                {"output_tokens": 42,
                 "iterations": [{"type": "fallback_message", "model": "m2"}]}),
            MESSAGE_STOP,
        ])
        self.assertEqual(data["stop_reason"], "end_turn")
        self.assertEqual(data["content"][0]["text"], "ok")
        self.assertEqual(data["container"]["id"], "c1")
        # call_claude reads exactly this to log a fallback-served response.
        self.assertEqual(data["usage"]["iterations"],
                         [{"type": "fallback_message", "model": "m2"}])

    def test_a_fallback_hop_relabels_the_model(self):
        """The response's `model` is not the one that was asked for once a
        fallback serves it, and the log line says so."""
        data = author._accumulate_stream([
            message_start(),
            block_start(0, {"type": "fallback", "to": {"model": "claude-fable-5"}}),
            block_stop(0),
            block_start(1, {"type": "text", "text": "done"}),
            block_stop(1),
            message_delta({"stop_reason": "end_turn"}, {"output_tokens": 4}),
            MESSAGE_STOP,
        ])
        self.assertEqual(data["model"], "claude-fable-5")

    def test_a_stream_with_no_message_start_is_an_author_error(self):
        """Not an AttributeError three frames deep: the pass says what
        happened and that a re-run is the answer."""
        with self.assertRaises(author.AuthorError) as caught:
            author._accumulate_stream([MESSAGE_STOP])
        self.assertIn("message_start", str(caught.exception))

    def test_unparseable_tool_json_is_an_author_error(self):
        with self.assertRaises(author.AuthorError) as caught:
            author._accumulate_stream([
                message_start(),
                block_start(0, {"type": "tool_use", "id": "t", "name": "x",
                                "input": {}}),
                block_delta(0, {"type": "input_json_delta",
                                "partial_json": '{"page": '}),
                block_stop(0),
            ])
        self.assertIn("did not parse as JSON", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
