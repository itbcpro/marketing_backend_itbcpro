"""Structured outputs: JSON-schema constrained responses validated with Pydantic."""

import copy
import json
import uuid
from typing import Any

from pydantic import BaseModel, ValidationError

from core.errors import DomainError, ErrorCode
from llm.ports import LlmRequest, LlmResponse, Message, TextBlock
from llm.service import LlmService

# Keywords the structured-output schema subset lacks; Pydantic enforces them after parsing.
_UNSUPPORTED = {
    "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minItems", "maxItems", "multipleOf", "default", "title",
}  # fmt: skip


class StructuredOutputError(DomainError):
    code = ErrorCode.STRUCTURED_OUTPUT_FAILED
    http_status = 502


def to_strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for ``model`` reduced to what constrained decoding accepts."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in [k for k in node if k in _UNSUPPORTED and not _is_property_name(node, k)]:
                del node[key]
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


def _is_property_name(node: dict[str, Any], key: str) -> bool:
    """A property called ``title`` or ``default`` lives under ``properties`` and must stay."""
    return "type" not in node and "properties" not in node and isinstance(node.get(key), dict)


def complete_structured[T: BaseModel](
    service: LlmService,
    request: LlmRequest,
    schema: type[T],
    *,
    max_repairs: int = 2,
    workspace_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    prompt_key: str = "",
) -> tuple[T, LlmResponse]:
    """Ask for JSON matching ``schema``; on invalid output show the error to the model and retry."""
    request.output_schema = to_strict_schema(schema)
    last_error = ""
    for _attempt in range(max_repairs + 1):
        response = service.complete(
            request, workspace_id=workspace_id, run_id=run_id, prompt_key=prompt_key
        )
        try:
            return schema.model_validate(json.loads(response.text)), response
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:1500]
            request.messages = [
                *request.messages,
                Message("assistant", [TextBlock(response.text or "{}")]),
                Message.user(
                    "That output was not valid for the required schema. Fix it and reply with "
                    f"only the corrected JSON. Problems: {last_error}"
                ),
            ]
    raise StructuredOutputError(last_error)
