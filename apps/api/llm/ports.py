"""Provider-neutral LLM types and the gateway port."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True, slots=True)
class OpaqueBlock:
    """A provider block (for example thinking) that must be echoed back unchanged."""

    raw: dict[str, Any]
    type: Literal["opaque"] = "opaque"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | OpaqueBlock


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant"]
    content: list[ContentBlock]

    @staticmethod
    def user(text: str) -> Message:
        return Message("user", [TextBlock(text)])


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SystemBlock:
    text: str
    cache: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(slots=True)
class LlmRequest:
    tier: str
    system: list[SystemBlock]
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 16000
    effort: str | None = None
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LlmResponse:
    content: list[ContentBlock]
    stop_reason: str
    usage: Usage
    model: str
    request_id: str = ""

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


class LlmGateway(Protocol):
    """Vendor adapter. Routing (tier to model) and accounting happen above this port."""

    def complete(self, request: LlmRequest, *, model: str) -> LlmResponse: ...
