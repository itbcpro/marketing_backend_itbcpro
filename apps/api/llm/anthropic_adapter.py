"""Anthropic Messages API adapter for the LLM gateway port."""

from typing import Any

import anthropic

from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from llm.ports import (
    ContentBlock,
    LlmRequest,
    LlmResponse,
    Message,
    OpaqueBlock,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

PROVIDER = "anthropic"
# Only these families accept output_config.effort; Haiku 4.5 rejects it.
EFFORT_PREFIXES = ("claude-opus", "claude-sonnet", "claude-fable", "claude-mythos")
# The SDK refuses a plain request that could run past ten minutes; large outputs must stream.
STREAM_ABOVE_TOKENS = 16000


def _block_to_api(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    return dict(block.raw)


def _messages_to_api(messages: list[Message]) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": [_block_to_api(b) for b in m.content]} for m in messages]


def _system_to_api(blocks: list[SystemBlock]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        entry: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            entry["cache_control"] = {"type": "ephemeral"}
        out.append(entry)
    return out


def _tools_to_api(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def build_kwargs(request: LlmRequest, model: str) -> dict[str, Any]:
    """Translate a neutral request into ``messages.create`` arguments.

    Thinking is never configured here: on the current models it is either always on (Opus 5.5)
    or off by default, and depth is controlled through ``effort``. Sampling parameters are not sent
    because current models reject them. Forced ``tool_choice`` is not used; structured output goes
    through ``output_config.format``.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_tokens,
        "system": _system_to_api(request.system),
        "messages": _messages_to_api(request.messages),
    }
    if request.tools:
        kwargs["tools"] = _tools_to_api(request.tools)
    output_config: dict[str, Any] = {}
    if request.effort and model.startswith(EFFORT_PREFIXES):
        output_config["effort"] = request.effort
    if request.output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
    if output_config:
        kwargs["output_config"] = output_config
    return kwargs


def _block_from_api(block: Any) -> ContentBlock:
    if block.type == "text":
        return TextBlock(block.text)
    if block.type == "tool_use":
        return ToolUseBlock(block.id, block.name, dict(block.input))
    return OpaqueBlock(block.model_dump(mode="json", exclude_none=True))


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitedError(str(exc), provider=PROVIDER)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AuthExpiredError(str(exc), provider=PROVIDER)
    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return RetryableProviderError(str(exc), provider=PROVIDER)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 529:  # noqa: PLR2004 - overloaded
            return RetryableProviderError(str(exc), provider=PROVIDER)
        return PermanentProviderError(str(exc), provider=PROVIDER)
    return exc


class AnthropicGateway:
    """Calls the Claude API. Pass ``client`` to inject a configured or fake client."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        api_key: str | None = None,
        workspace_id: str = "",
    ) -> None:
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._client = client or anthropic.Anthropic(
            api_key=api_key or None, default_headers=headers
        )

    def complete(self, request: LlmRequest, *, model: str) -> LlmResponse:
        kwargs = build_kwargs(request, model)
        try:
            if request.max_tokens > STREAM_ABOVE_TOKENS:
                with self._client.messages.stream(**kwargs) as stream:
                    response = stream.get_final_message()
            else:
                response = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc
        usage = response.usage
        return LlmResponse(
            content=[_block_from_api(b) for b in response.content],
            stop_reason=str(response.stop_reason or ""),
            usage=Usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            model=str(response.model),
            request_id=str(getattr(response, "_request_id", "") or ""),
        )
