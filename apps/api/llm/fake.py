"""Deterministic gateway for tests: replays scripted responses and records every request."""

from collections.abc import Callable
from dataclasses import dataclass, field

from llm.ports import LlmRequest, LlmResponse, TextBlock, ToolUseBlock, Usage

Scripted = LlmResponse | Callable[[LlmRequest], LlmResponse] | Exception


def text_response(
    text: str, *, model: str = "fake-model", usage: Usage | None = None
) -> LlmResponse:
    return LlmResponse(
        content=[TextBlock(text)],
        stop_reason="end_turn",
        usage=usage or Usage(input_tokens=100, output_tokens=20),
        model=model,
    )


def tool_response(
    name: str, tool_input: dict[str, object], *, call_id: str = "toolu_1", text: str = ""
) -> LlmResponse:
    blocks: list[TextBlock | ToolUseBlock] = [TextBlock(text)] if text else []
    blocks.append(ToolUseBlock(call_id, name, dict(tool_input)))
    return LlmResponse(
        content=list(blocks),
        stop_reason="tool_use",
        usage=Usage(input_tokens=120, output_tokens=30),
        model="fake-model",
    )


@dataclass
class FakeGateway:
    script: list[Scripted] = field(default_factory=list)
    requests: list[tuple[LlmRequest, str]] = field(default_factory=list)

    def complete(self, request: LlmRequest, *, model: str) -> LlmResponse:
        self.requests.append((request, model))
        if not self.script:
            raise AssertionError("FakeGateway script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step
