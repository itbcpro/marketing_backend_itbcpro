"""AI-written alternative versions of an ad; choosing one edits the ad through the action layer."""

import json
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from ninja import Schema
from pydantic import Field, field_validator

from ads import providers
from ads.models import AdEntity, AdLevel, AdReview, AdVariantSet
from ads.openai import changes, fixes
from ads.openai.review import AGENT, ad_snapshot
from connections.models import Connection
from core.errors import ConflictError, DomainError, ErrorCode, NotFoundError, ValidationFailedError
from core.logging import get_logger
from identity.models import User
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory

log = get_logger(__name__)

PROMPT_KEY = "ads/openai-variants"
VARIANT_COUNT = 3
MAX_INSTRUCTION_CHARS = 1000
STUCK_AFTER = timedelta(minutes=10)
TITLE_MAX = 50
BODY_MAX = 100


class Variant(Schema):
    title: str = Field(min_length=3, max_length=TITLE_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)
    angle: str
    rationale: str


class VariantResult(Schema):
    variants: list[Variant] = Field(min_length=VARIANT_COUNT, max_length=VARIANT_COUNT)


RSA_PROMPT_KEY = "ads/google-rsa"
RSA_HEADLINE_MAX = 30
RSA_DESCRIPTION_MAX = 90


class RsaVariant(Schema):
    """A full responsive search ad text set: up to 15 headlines and 4 descriptions."""

    headlines: list[str] = Field(min_length=8, max_length=15)
    descriptions: list[str] = Field(min_length=3, max_length=4)
    angle: str
    rationale: str

    @field_validator("headlines")
    @classmethod
    def headline_rules(cls, value: list[str]) -> list[str]:
        clean = [h.strip() for h in value]
        if any(not 1 <= len(h) <= RSA_HEADLINE_MAX for h in clean):
            raise ValueError(f"every headline must be 1 to {RSA_HEADLINE_MAX} characters")
        if len({h.lower() for h in clean}) != len(clean):
            raise ValueError("headlines must all be different")
        return clean

    @field_validator("descriptions")
    @classmethod
    def description_rules(cls, value: list[str]) -> list[str]:
        clean = [d.strip() for d in value]
        if any(not 1 <= len(d) <= RSA_DESCRIPTION_MAX for d in clean):
            raise ValueError(f"every description must be 1 to {RSA_DESCRIPTION_MAX} characters")
        if len({d.lower() for d in clean}) != len(clean):
            raise ValueError("descriptions must all be different")
        return clean


class RsaVariantResult(Schema):
    variants: list[RsaVariant] = Field(min_length=VARIANT_COUNT, max_length=VARIANT_COUNT)


def _enqueue(variant_set: AdVariantSet) -> None:
    from ads.tasks import ad_variants_task

    transaction.on_commit(lambda: ad_variants_task.delay(str(variant_set.pk)))


def start(
    connection: Connection, user_id: uuid.UUID, entity: AdEntity, instruction: str
) -> AdVariantSet:
    if entity.level != AdLevel.AD:
        raise ValidationFailedError("versions can only be written for an ad")
    text = instruction.strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise ValidationFailedError("the request is too long")
    creative = entity.data.get("creative") or {}
    expected = "responsive_search_ad" if connection.provider == providers.GOOGLE else "chat_card"
    if creative.get("type") != expected:
        raise ValidationFailedError("this creative type cannot be rewritten here")
    busy = AdVariantSet.objects.filter(entity=entity, status="thinking").first()
    if busy and busy.thinking_since and timezone.now() - busy.thinking_since < STUCK_AFTER:
        return busy
    variant_set = AdVariantSet.objects.create(
        workspace_id=connection.workspace_id,
        connection=connection,
        entity=entity,
        created_by_id=user_id,
        instruction=text,
        thinking_since=timezone.now(),
    )
    _enqueue(variant_set)
    return variant_set


def get(workspace_id: uuid.UUID, set_id: uuid.UUID) -> AdVariantSet:
    found = AdVariantSet.objects.filter(pk=set_id, workspace_id=workspace_id).first()
    if found is None:
        raise NotFoundError("versions not found")
    if (
        found.status == "thinking"
        and found.thinking_since
        and timezone.now() - found.thinking_since > STUCK_AFTER
    ):
        found.status = "failed"
        found.error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        found.save(update_fields=["status", "error_code", "updated_at"])
    return found


def run(set_id: uuid.UUID) -> str:
    variant_set = (
        AdVariantSet.objects.select_related("workspace", "connection", "entity__parent__parent")
        .filter(pk=set_id)
        .first()
    )
    if variant_set is None or variant_set.status != "thinking":
        return "skipped"
    entity = variant_set.entity
    try:
        last = AdReview.objects.filter(entity=entity, status="done").order_by("-created_at").first()
        google = variant_set.connection.provider == providers.GOOGLE
        key = RSA_PROMPT_KEY if google else PROMPT_KEY
        prompt = prompts.get_prompt(key)
        workspace = variant_set.workspace
        system = prompt.render(
            workspace_name=workspace.name,
            brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
            memories=memory.render_memories(
                memory.retrieve(
                    workspace_id=workspace.pk, query=f"ad copy {entity.name}", agent=AGENT, limit=6
                )
            ),
            snapshot=json.dumps(ad_snapshot(entity), indent=1, default=str),
            last_review=json.dumps(last.result, indent=1) if last and last.result else "",
            instruction=variant_set.instruction,
        )
        request = LlmRequest(
            tier=prompt.tier,
            system=[SystemBlock(system)],
            messages=[Message.user("Answer with the three versions.")],
        )
        result, _ = complete_structured(
            get_llm_service(workspace.pk),
            request,
            RsaVariantResult if google else VariantResult,
            workspace_id=workspace.pk,
            prompt_key=f"{key}@v{prompt.version}",
        )
    except DomainError as exc:
        return _fail(variant_set, exc.code.value)
    except Exception:
        log.exception("ad_variants_crashed")
        return _fail(variant_set, ErrorCode.UNKNOWN.value)
    written: RsaVariantResult | VariantResult = result  # type: ignore[assignment]
    variant_set.variants = [v.model_dump() for v in written.variants]
    variant_set.status = "done"
    variant_set.thinking_since = None
    variant_set.save()
    return "done"


def _fail(variant_set: AdVariantSet, code: str) -> str:
    variant_set.status = "failed"
    variant_set.error_code = code
    variant_set.thinking_since = None
    variant_set.save(update_fields=["status", "error_code", "thinking_since", "updated_at"])
    return "failed"


def choose(variant_set: AdVariantSet, user: User, index: int) -> tuple[AdVariantSet, bool]:
    """Replace the ad's text with the chosen version. Applied now when the user may approve."""
    if variant_set.status != "done" or not 0 <= index < len(variant_set.variants):
        raise ValidationFailedError("choose one of the suggested versions")
    if variant_set.chosen_index is not None:
        raise ConflictError("a version was already chosen")
    chosen = variant_set.variants[index]
    if variant_set.connection.provider == providers.GOOGLE:
        kind = "ad.update_rsa"
        payload = {
            "ad_id": variant_set.entity.external_id,
            "headlines": chosen["headlines"],
            "descriptions": chosen["descriptions"],
        }
    else:
        kind, payload, _ = fixes.build_change(
            variant_set.entity,
            {"type": "ad_text", "new_title": chosen["title"], "new_body": chosen["body"]},
        )
    action, applied = changes.submit(
        variant_set.connection,
        user,
        kind,
        payload,
        summary=f"Replace the ad text with the AI version: {chosen['angle']}"[:300],
        apply_now=True,
    )
    variant_set.chosen_index = index
    variant_set.action_id = action.pk
    variant_set.applied = applied
    variant_set.save()
    return variant_set, applied
