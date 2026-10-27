

from llm_clients.connector import LLMConnector


def _run(connector, monkeypatch, **kwargs):
    """Capture the Responses body the connector would enqueue."""
    captured = {}

    def _fake_job(body):
        captured.update(body)
        return {"status": "done", "result": {"id": "r", "output": [], "usage": {}}}

    monkeypatch.setattr(connector, "_run_job", _fake_job)
    connector.generate_with_tools([{"role": "user", "content": "hi"}], **kwargs)
    return captured


def test_the_canonical_payload_carries_a_plain_effort_string(monkeypatch):
    payload = _run(LLMConnector("e", "m", 32768, reasoning="low"), monkeypatch)
    assert payload["reasoning"] == "low"
    # canonical = the chat shape; the worker turns it into whatever its target serves
    assert "messages" in payload and "input" not in payload


def test_reasoning_omitted_when_unset(monkeypatch):
    assert "reasoning" not in _run(LLMConnector("e", "m", 32768), monkeypatch)


def test_per_call_reasoning_overrides_configured(monkeypatch):
    # The executor escalates effort on a stalled target regardless of the configured floor.
    c = LLMConnector("e", "m", 32768, reasoning="none")
    assert _run(c, monkeypatch, reasoning="high")["reasoning"] == "high"


def test_per_call_reasoning_aliases_resolve(monkeypatch):
    c = LLMConnector("e", "m", 32768, reasoning="none")
    assert _run(c, monkeypatch, reasoning="on")["reasoning"] == "high"


def test_per_call_none_disables_configured_reasoning(monkeypatch):
    # "none" is the explicit OFF effort (the API rejects omitting it as "off"), so a per-call
    # "none" must send effort:none even when the connector is configured high.
    c = LLMConnector("e", "m", 32768, reasoning="high")
    assert _run(c, monkeypatch, reasoning="none")["reasoning"] == "none"


def test_unset_per_call_falls_back_to_configured(monkeypatch):
    payload = _run(LLMConnector("e", "m", 32768, reasoning="low"), monkeypatch)
    assert payload["reasoning"] == "low"


def test_reasoning_aliases_on_off_to_effort_enum():
    assert LLMConnector("e", "m", 32768, reasoning="on").reasoning == "high"
    assert LLMConnector("e", "m", 32768, reasoning="off").reasoning == "none"
    assert LLMConnector("e", "m", 32768, reasoning="low").reasoning == "low"


def test_payload_omits_an_unset_output_cap():
    """A cap is subtracted from what the input may use, so a caller must be able to decline one."""
    assert "max_tokens" not in LLMConnector("e", "m", 32768, max_tokens=None)._payload([], None, None)
    assert LLMConnector("e", "m", 32768)._payload([], None, None)["max_tokens"] == 50000


def test_a_named_temperature_reaches_the_payload():
    """Worldgen stages pick a temperature per stage; an unnamed one keeps the default."""
    c = LLMConnector("e", "m", 32768)
    assert c._payload([], None, None)["temperature"] == 0.7
    assert c._payload([], None, None, temperature=0.0)["temperature"] == 0.0
