"""Scripted fake Anthropic client and response builders for the RC-23a tests.

Nothing here imports the real `anthropic` package, so every test that uses these
helpers behaves the same whether or not the SDK is installed.
"""
import json
import time
from types import SimpleNamespace


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def thinking_block(thinking="considering the titles"):
    # Real thinking blocks carry `.thinking`, not `.text`.
    return SimpleNamespace(type="thinking", thinking=thinking)


def make_response(blocks, *, stop_reason="end_turn", input_tokens=10, output_tokens=5, usage="default"):
    if usage == "default":
        usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    response = SimpleNamespace(content=list(blocks), usage=usage)
    if stop_reason != "MISSING":
        response.stop_reason = stop_reason
    return response


def classifications_json(rows):
    """rows: list of (index, item_id, confidence, evidence) tuples."""
    return json.dumps({
        "classifications": [
            {"index": i, "item_id": item, "confidence": conf, "evidence": ev}
            for i, item, conf, ev in rows
        ]
    }, ensure_ascii=False)


def ok_response(rows, *, input_tokens=10, output_tokens=5, thinking=True):
    blocks = ([thinking_block()] if thinking else []) + [text_block(classifications_json(rows))]
    return make_response(blocks, input_tokens=input_tokens, output_tokens=output_tokens)


def payload_of(kwargs):
    return json.loads(kwargs["messages"][0]["content"])


def schema_of(kwargs):
    return kwargs["output_config"]["format"]["schema"]


def schema_item_ids(kwargs):
    """The item_id enum of a request's schema, as a list (ends with None)."""
    props = schema_of(kwargs)["properties"]["classifications"]["items"]["properties"]
    return props["item_id"]["enum"]


def responder(table, *, input_tokens=10, output_tokens=5, default=(None, "high")):
    """A callable script entry: answers each title from `table` (title -> (item_id, confidence)).

    Titles missing from the table get `default`.  Answers are built from the payload, so
    the test does not depend on how the implementation orders or chunks its requests.
    """
    def build(kwargs):
        rows = []
        for entry in payload_of(kwargs)["titles"]:
            item_id, confidence = table.get(entry["title"], default)
            rows.append((entry["index"], item_id, confidence, "evidence-text"))
        return ok_response(rows, input_tokens=input_tokens, output_tokens=output_tokens)
    return build


class _Messages:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        return self._owner._create(kwargs)


class FakeClient:
    """`client.messages.create(**kwargs)` returns or raises the next scripted entry.

    A script entry is a response object, an Exception instance (raised), or a callable
    taking the create() kwargs and returning a response.  An exhausted script raises
    AssertionError so an unexpected extra request fails loudly.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.messages = _Messages(self)
        self._in_flight = 0
        self.max_in_flight = 0

    def _create(self, kwargs):
        self.calls.append(kwargs)
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(0.005)  # lets an accidentally concurrent caller overlap
            if not self.script:
                raise AssertionError("FakeClient script exhausted: unexpected extra request")
            entry = self.script.pop(0)
            if isinstance(entry, BaseException):
                raise entry
            if callable(entry):
                return entry(kwargs)
            return entry
        finally:
            self._in_flight -= 1


class ForbiddenFactory:
    """A client_factory that must never be called.  Records any call, then raises."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("a client must not be created in this scenario")


class ClientFactory:
    """A client_factory returning one scripted FakeClient and recording its calls."""

    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.client
