"""Persist and restore conversation blocks (used to resume agent runs)."""

from typing import Any

from llm.ports import (
    ContentBlock,
    OpaqueBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def blocks_to_json(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            out.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
        else:
            out.append({"type": "opaque", "raw": block.raw})
    return out


def blocks_from_json(data: list[dict[str, Any]]) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    for item in data:
        kind = item["type"]
        if kind == "text":
            blocks.append(TextBlock(item["text"]))
        elif kind == "tool_use":
            blocks.append(ToolUseBlock(item["id"], item["name"], item["input"]))
        elif kind == "tool_result":
            blocks.append(
                ToolResultBlock(item["tool_use_id"], item["content"], bool(item.get("is_error")))
            )
        else:
            blocks.append(OpaqueBlock(item["raw"]))
    return blocks
