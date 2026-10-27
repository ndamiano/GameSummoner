import json

from pydantic import BaseModel

from maestro.worldgen import llm


class FakeConnector:
    """Scripted responses, returned in order; records every payload it is given."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_with_tools(self, messages, tools=None, reasoning=None, max_tokens=None,
                            temperature=None):
        self.calls.append({"messages": messages, "tools": tools, "reasoning": reasoning,
                           "temperature": temperature})
        return self.replies.pop(0)


def test_the_fake_accepts_exactly_what_the_connector_does():
    """The harness is tested against this fake, so a kwarg the real connector drops must fail
    here, not in a build."""
    import inspect
    from llm_clients.connector import LLMConnector
    real = set(inspect.signature(LLMConnector.generate_with_tools).parameters) - {"self"}
    fake = set(inspect.signature(FakeConnector.generate_with_tools).parameters) - {"self"}
    assert fake == real


def _reply(content=None, tool_calls=None):
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": {}}


def test_no_tool_call_is_one_round_trip(monkeypatch):
    fake = FakeConnector([_reply("hello there")])
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    resp = llm.LLMHarness().send_message_with_tools("hi")

    assert resp.text == "hello there"
    assert str(resp) == "hello there"
    assert fake.calls[0]["reasoning"] == "none"
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_tool_loop_dispatches_then_finishes(monkeypatch):
    calls = []

    @llm.tool
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        calls.append((a, b))
        return str(a + b)

    replies = [
        _reply(tool_calls=[{"id": "1", "type": "function",
                             "function": {"name": "add", "arguments": json.dumps({"a": 1, "b": 2})}}]),
        _reply(tool_calls=[{"id": "2", "type": "function",
                             "function": {"name": "add", "arguments": json.dumps({"a": 3, "b": 4})}}]),
        _reply("done"),
    ]
    fake = FakeConnector(replies)
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    resp = llm.LLMHarness(tools=[add]).send_message_with_tools("go")

    assert calls == [(1, 2), (3, 4)]
    assert resp.text == "done"
    assert len(fake.calls) == 3


def test_pydantic_tool_argument_is_deserialized(monkeypatch):
    class Box(BaseModel):
        label: str
        size_m: float

    received = []

    @llm.tool
    def add_box(box: Box) -> str:
        """Record a box."""
        received.append(box)
        return "ok"

    replies = [
        _reply(tool_calls=[{"id": "1", "type": "function", "function": {
            "name": "add_box",
            "arguments": json.dumps({"box": {"label": "crate", "size_m": 1.5}}),
        }}]),
        _reply("done"),
    ]
    fake = FakeConnector(replies)
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    llm.LLMHarness(tools=[add_box]).send_message_with_tools("go")

    assert len(received) == 1
    assert isinstance(received[0], Box)
    assert received[0].label == "crate"


def test_image_parts_reach_the_outgoing_payload(monkeypatch):
    fake = FakeConnector([_reply("ok")])
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    message = llm.Message("user", [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])
    llm.LLMHarness().send_message_with_tools([message])

    sent_content = fake.calls[0]["messages"][0]["content"]
    assert sent_content == message.content
    assert sent_content[1]["type"] == "image_url"


def test_transcript_available_on_raw_messages(monkeypatch):
    replies = [
        _reply(tool_calls=[{"id": "1", "type": "function",
                             "function": {"name": "finish", "arguments": "{}"}}]),
        _reply("all done"),
    ]
    fake = FakeConnector(replies)
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    @llm.tool
    def finish() -> str:
        """Finish."""
        return "finished"

    resp = llm.LLMHarness(tools=[finish]).send_message_with_tools("go")

    roles = [m.role for m in resp.raw["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert resp.raw["messages"][-1].content == "all done"


def test_temperature_reaches_every_call(monkeypatch):
    """Each stage sets its own temperature; the grounding pass is greedy and must stay so."""

    @llm.tool
    def note(text: str) -> str:
        """Record something."""
        return "ok"

    replies = [
        _reply(tool_calls=[{"id": "1", "type": "function",
                            "function": {"name": "note", "arguments": json.dumps({"text": "x"})}}]),
        _reply("done"),
    ]
    fake = FakeConnector(replies)
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    llm.LLMHarness(tools=[note], temperature=0.0).send_message_with_tools("go")

    assert [c["temperature"] for c in fake.calls] == [0.0, 0.0]


def test_temperature_omitted_leaves_the_connector_default(monkeypatch):
    fake = FakeConnector([_reply("hi")])
    monkeypatch.setattr(llm, "_get_connector", lambda name: fake)

    llm.LLMHarness().send_message_with_tools("hi")

    assert fake.calls[0]["temperature"] is None
