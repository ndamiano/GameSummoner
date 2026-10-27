"""LLM calls for worldgen: its tool loop over the `llm` queue, whose worker picks the server."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict

from llm_clients.connector import LLMConnector

from . import WORLD_JOB_TIMEOUT
from .llm_tools import Tool, as_tool, tool

MAX_TOOL_ROUNDS = 10

_world_connectors: dict[str, LLMConnector] = {}


def _get_connector(name: str) -> LLMConnector:
    """Not the shared connector: its 900 s job timeout is shorter than a wait behind a model swap."""
    if name not in _world_connectors:
        _world_connectors[name] = LLMConnector.for_entry(
            name,
            # No output cap: a worldgen prompt is a transcript plus several renders, and a cap
            # that is most of the window leaves the input the smaller half of it.
            max_tokens=None,
            job_timeout_seconds=WORLD_JOB_TIMEOUT)
    return _world_connectors[name]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: str
    content: Any = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.text


def _to_response(result: dict) -> Response:
    from maestro.services import parse_args_checked

    if "error" in result and "choices" not in result:
        raise RuntimeError(f"llm queue call failed: {result['error']}")
    message = result["choices"][0]["message"]
    tool_calls = [
        ToolCall(
            id=tc["id"],
            name=tc["function"]["name"],
            # Arguments the harness cannot read reach the tool as {}, which fails its binding and
            # answers the model with the error — the loop's own way back. A raise here would take
            # the whole world stage down with one malformed call.
            arguments=parse_args_checked(tc["function"]["arguments"])[0],
        )
        for tc in message.get("tool_calls") or []
    ]
    return Response(text=message.get("content") or "", tool_calls=tool_calls, raw=result)


class Strict(BaseModel):
    """Reject unknown keys instead of silently dropping them."""

    model_config = ConfigDict(extra="forbid")


def image_part(path: Path) -> dict:
    """One render, as the content part an OpenAI-compatible endpoint expects."""
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


class LLMHarness:
    """Minimal-overhead LLM client over the maestro queue.

        h = LLMHarness(tools=[get_weather], system="...")
        h.send_message_with_tools("weather in Paris?")

    Args:
        tools: default tools available to every call.
        system: default system prompt.
        temperature: sampling temperature for every call. Each worldgen stage sets its
            own — the grounding pass is greedy, the planners are not — so a harness that
            dropped it would run every stage at the connector's one default.
        max_tool_rounds: cap on tool-loop rounds before the loop stops asking.
    """

    def __init__(
        self,
        tools: Sequence[Any] | None = None,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.system = system
        self.temperature = temperature
        self.max_tool_rounds = max_tool_rounds
        self.tools: list[Tool] = [as_tool(t) for t in (tools or [])]

    def _build(self, prompt: str | list[Message] | None, system: str | None) -> list[Message]:
        msgs: list[Message] = []
        sys_prompt = system if system is not None else self.system
        if sys_prompt:
            msgs.append(Message("system", sys_prompt))
        if isinstance(prompt, str):
            msgs.append(Message("user", prompt))
        elif prompt:
            msgs += prompt
        return msgs

    @staticmethod
    def _run_tool(tool_: Tool, call: ToolCall) -> str:
        if tool_.fn is None:
            return f"error: tool {call.name} has no implementation bound"
        try:
            result = tool_.fn(**tool_.bind(call.arguments))
        except Exception as e:  # surface failures to the model, don't crash the loop
            return f"error: {type(e).__name__}: {e}"
        return result if isinstance(result, str) else json.dumps(result, default=str)

    def send_message_with_tools(
        self,
        prompt: str | list[Message] | None = None,
        *,
        tools: Sequence[Any] | None = None,
        system: str | None = None,
        max_rounds: int | None = None,
    ) -> Response:
        """Run the tool loop until the model answers without calling a tool.

        `response.raw["messages"]` holds the full transcript of the loop.
        """
        tool_list = [as_tool(t) for t in tools] if tools is not None else self.tools
        by_name = {t.name: t for t in tool_list}
        schema = [t.to_json_schema() for t in tool_list]
        msgs = self._build(prompt, system)
        rounds = max_rounds if max_rounds is not None else self.max_tool_rounds
        connector = _get_connector("qwen-flash-next")

        def ask() -> Response:
            return _to_response(connector.generate_with_tools(
                [m.to_dict() for m in msgs], tools=schema, reasoning="none",
                temperature=self.temperature,
            ))

        resp = ask()
        for _ in range(rounds):
            if not resp.tool_calls:
                break
            msgs.append(Message("assistant", resp.text or None, tool_calls=resp.tool_calls))
            for call in resp.tool_calls:
                t = by_name.get(call.name)
                out = (
                    f"error: unknown tool {call.name}"
                    if t is None
                    else self._run_tool(t, call)
                )
                msgs.append(Message("tool", out, tool_call_id=call.id, name=call.name))
            resp = ask()

        msgs.append(Message("assistant", resp.text))
        resp.raw = {**resp.raw, "messages": msgs}
        return resp


__all__ = ["LLMHarness", "Message", "Response", "Strict", "ToolCall", "image_part", "tool", "as_tool", "Tool"]
