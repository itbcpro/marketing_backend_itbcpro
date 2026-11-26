import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from django.test import override_settings
from pydantic import BaseModel, Field

from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from llm import prompts
from llm.anthropic_adapter import AnthropicGateway, build_kwargs
from llm.fake import FakeGateway, text_response
from llm.models import LlmCall
from llm.ports import (
    LlmRequest,
    Message,
    OpaqueBlock,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from llm.pricing import cost_micros
from llm.service import BudgetExceededError, LlmService
from llm.structured import StructuredOutputError, complete_structured, to_strict_schema

pytestmark = pytest.mark.django_db


def _request(**kw) -> LlmRequest:
    return LlmRequest(
        tier="writer", system=[SystemBlock("sys", cache=True)], messages=[Message.user("hi")], **kw
    )


# ---- adapter request/response mapping -----------------------------------------------------------


def test_request_translation_uses_current_api_shapes() -> None:
    request = _request(
        tools=[ToolSpec("lookup", "Find things", {"type": "object", "properties": {}})],
        effort="high",
        output_schema={"type": "object"},
    )
    request.messages.append(
        Message(
            "assistant",
            [
                OpaqueBlock({"type": "thinking", "thinking": "", "signature": "sig"}),
                ToolUseBlock("t1", "lookup", {"q": 1}),
            ],
        )
    )
    request.messages.append(Message("user", [ToolResultBlock("t1", "found", is_error=False)]))
    kwargs = build_kwargs(request, "claude-opus-5-5")

    assert kwargs["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert kwargs["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert kwargs["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assistant = kwargs["messages"][1]["content"]
    assert assistant[0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig",
    }  # echoed unchanged
    assert kwargs["messages"][2]["content"][0]["tool_use_id"] == "t1"
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert forbidden not in kwargs


def test_effort_is_only_sent_to_models_that_accept_it() -> None:
    request = _request(effort="low")
    assert "output_config" in build_kwargs(request, "claude-sonnet-5-5")
    assert "output_config" not in build_kwargs(request, "claude-haiku-4-5-20251001")


class _FakeMessages:
    def __init__(self, response=None, error=None) -> None:
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _api_response():
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                model_dump=lambda **_: {"type": "thinking", "thinking": "", "signature": "s"},
            ),
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="tool_use", id="toolu_9", name="lookup", input={"q": "x"}),
        ],
        stop_reason="tool_use",
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
        ),
        _request_id="req_1",
    )


def test_response_mapping_keeps_thinking_blocks_opaque() -> None:
    gateway = AnthropicGateway(client=SimpleNamespace(messages=_FakeMessages(_api_response())))
    response = gateway.complete(_request(), model="claude-opus-5-5")
    assert response.text == "hello" and response.stop_reason == "tool_use"
    assert response.tool_uses[0].input == {"q": "x"}
    assert (
        isinstance(response.content[0], OpaqueBlock) and response.content[0].raw["signature"] == "s"
    )
    assert response.usage == Usage(10, 5, 3, 2) and response.request_id == "req_1"


class _FakeStream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._response


def test_large_outputs_are_streamed() -> None:
    calls = []

    class Messages(_FakeMessages):
        def stream(self, **kwargs):
            calls.append(kwargs["max_tokens"])
            return _FakeStream(self.response)

    gateway = AnthropicGateway(client=SimpleNamespace(messages=Messages(_api_response())))
    response = gateway.complete(_request(max_tokens=32000), model="claude-opus-5-5")
    assert response.text == "hello" and calls == [32000]


def _http_error(cls, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_http_error(anthropic.RateLimitError, 429), RateLimitedError),
        (_http_error(anthropic.AuthenticationError, 401), AuthExpiredError),
        (_http_error(anthropic.InternalServerError, 500), RetryableProviderError),
        (_http_error(anthropic.BadRequestError, 400), PermanentProviderError),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            RetryableProviderError,
        ),
    ],
)
def test_sdk_errors_map_to_the_provider_taxonomy(error, expected) -> None:
    gateway = AnthropicGateway(client=SimpleNamespace(messages=_FakeMessages(error=error)))
    with pytest.raises(expected):
        gateway.complete(_request(), model="claude-opus-5-5")


# ---- pricing, routing, ledger, budget -----------------------------------------------------------


def test_pricing_known_and_unknown_models() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=0,
    )
    assert cost_micros("claude-opus-5-5", usage) == (4_000_000 + 20_000_000 + 200_000, True)
    assert cost_micros("claude-sonnet-5-5", Usage(500_000, 100_000)) == (2_000_000, True)
    assert cost_micros("future-model", usage) == (0, False)


@override_settings(
    MODEL_ROUTING={"writer": "claude-sonnet-5-5"}, EFFORT_BY_TIER={"writer": "medium"}
)
def test_service_routes_tiers_and_records_the_ledger(workspace) -> None:
    gateway = FakeGateway([text_response("ok", model="claude-sonnet-5-5", usage=Usage(1000, 500))])
    response = LlmService(gateway).complete(
        _request(), workspace_id=workspace.pk, prompt_key="x/y@v1"
    )
    assert response.text == "ok"
    request, model = gateway.requests[0]
    assert model == "claude-sonnet-5-5" and request.effort == "medium"
    call = LlmCall.objects.get()
    assert (call.workspace_id, call.prompt_key, call.input_tokens, call.output_tokens) == (
        workspace.pk,
        "x/y@v1",
        1000,
        500,
    )
    assert call.cost_micros == 7000 and call.cost_known


@override_settings(MODEL_ROUTING={"writer": "m"})
def test_provider_errors_are_recorded_and_reraised(workspace) -> None:
    gateway = FakeGateway([RetryableProviderError("down", provider="anthropic")])
    with pytest.raises(RetryableProviderError):
        LlmService(gateway).complete(_request(), workspace_id=workspace.pk)
    assert LlmCall.objects.get().error_code == "provider_unavailable"


@override_settings(MODEL_ROUTING={"writer": "claude-sonnet-5-5"}, LLM_DAILY_BUDGET_MICROS=5000)
def test_daily_budget_blocks_further_calls(workspace) -> None:
    gateway = FakeGateway(
        [text_response("a", model="claude-sonnet-5-5", usage=Usage(1000, 500)), text_response("b")]
    )
    service = LlmService(gateway)
    service.complete(_request(), workspace_id=workspace.pk)  # 7000 micros spent
    with pytest.raises(BudgetExceededError):
        service.complete(_request(), workspace_id=workspace.pk)
    assert len(gateway.requests) == 1


def test_unknown_tier_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown model tier"):
        LlmService(FakeGateway()).complete(LlmRequest(tier="nope", system=[], messages=[]))


# ---- structured output --------------------------------------------------------------------------


class Brief(BaseModel):
    intent: str
    budget_micros: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=3, description="human title")


@override_settings(MODEL_ROUTING={"analyst": "claude-sonnet-5-5"})
def test_structured_output_validates_and_repairs() -> None:
    gateway = FakeGateway(
        [
            text_response("not json"),
            text_response(json.dumps({"intent": "campaign", "title": "x"})),
            text_response(json.dumps({"intent": "campaign", "title": "Launch"})),
        ]
    )
    request = LlmRequest(tier="analyst", system=[], messages=[Message.user("go")])
    brief, _ = complete_structured(LlmService(gateway), request, Brief)
    assert brief.title == "Launch" and len(gateway.requests) == 3
    repair = gateway.requests[2][0].messages[-1].content[0]
    assert isinstance(repair, TextBlock) and "not valid" in repair.text


@override_settings(MODEL_ROUTING={"analyst": "claude-sonnet-5-5"})
def test_structured_output_gives_up_after_bounded_repairs() -> None:
    gateway = FakeGateway([text_response("nope")] * 3)
    with pytest.raises(StructuredOutputError):
        complete_structured(
            LlmService(gateway),
            LlmRequest(tier="analyst", system=[], messages=[Message.user("go")]),
            Brief,
        )
    assert len(gateway.requests) == 3


def test_strict_schema_forbids_extra_keys_and_drops_unsupported_keywords() -> None:
    class Nested(BaseModel):
        title: str  # a property called "title" must survive
        n: int = Field(ge=1, le=9)

    class Outer(BaseModel):
        item: Nested
        tags: list[str] = Field(min_length=1)

    schema = to_strict_schema(Outer)
    assert schema["additionalProperties"] is False
    nested = schema["$defs"]["Nested"]
    assert nested["additionalProperties"] is False and "title" in nested["properties"]
    assert "minimum" not in json.dumps(schema) and "maximum" not in json.dumps(schema)
    assert "minItems" not in json.dumps(schema)


# ---- prompt files -------------------------------------------------------------------------------


def _write(tmp_path, key: str, version: int, body: str) -> None:
    path = tmp_path / key
    path.mkdir(parents=True, exist_ok=True)
    (path / f"v{version}.md").write_text(body, encoding="utf-8")


GOOD = '+++\npurpose = "p"\ntier = "writer"\nvariables = ["name"]\n+++\nHello ${name}\n'


def test_prompts_render_select_versions_and_validate_variables(tmp_path) -> None:
    _write(tmp_path, "d/a", 1, GOOD)
    _write(tmp_path, "d/a", 2, GOOD.replace("Hello", "Hi"))
    with override_settings(PROMPTS_DIR=tmp_path):
        prompts.clear_cache()
        assert prompts.get_prompt("d/a").render(name="Sam") == "Hi Sam\n"
        assert prompts.get_prompt("d/a", 1).render(name="Sam") == "Hello Sam\n"
        assert prompts.available_versions("d/a") == [1, 2]
        with pytest.raises(prompts.PromptError, match="missing"):
            prompts.get_prompt("d/a").render()
        with pytest.raises(prompts.PromptError, match="unknown"):
            prompts.get_prompt("d/a").render(name="x", extra="y")
        with pytest.raises(prompts.PromptError):
            prompts.get_prompt("d/a", 9)
        with pytest.raises(prompts.PromptError):
            prompts.get_prompt("does/not/exist")


@pytest.mark.parametrize(
    "body",
    [
        "no front matter",
        '+++\ntier = "writer"\n+++\nbody',
        '+++\npurpose = "p"\ntier = "writer"\nvariables = ["a"]\n+++\nuses ${b}',
    ],
)
def test_malformed_prompts_are_rejected(tmp_path, body: str) -> None:
    _write(tmp_path, "d/bad", 1, body)
    with override_settings(PROMPTS_DIR=tmp_path):
        prompts.clear_cache()
        with pytest.raises(prompts.PromptError):
            prompts.get_prompt("d/bad")


def test_placeholders_cannot_execute_code(tmp_path) -> None:
    _write(
        tmp_path, "d/safe", 1, '+++\npurpose = "p"\ntier = "writer"\nvariables = ["x"]\n+++\n${x}\n'
    )
    with override_settings(PROMPTS_DIR=tmp_path):
        prompts.clear_cache()
        assert prompts.get_prompt("d/safe").render(x="${__import__}") == "${__import__}\n"


def test_every_prompt_in_the_repository_loads_and_uses_a_known_tier() -> None:
    from django.conf import settings

    prompts.clear_cache()
    keys = prompts.all_prompt_keys()
    assert {"agents/orchestrator", "command/brief", "memory/reflection"} <= set(keys)
    for key in keys:
        prompt = prompts.get_prompt(key)
        assert prompt.tier in settings.MODEL_ROUTING, key


def test_probe_sends_workspace_header_when_given():
    import httpx

    from llm import provider

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": []})

    provider._probe("sk-ant-test", "wrkspc_abc", transport=httpx.MockTransport(handler))
    assert seen["anthropic-workspace-id"] == "wrkspc_abc"
