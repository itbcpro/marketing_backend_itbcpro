"""Root Ninja API: routers, authentication default and uniform problem responses."""

from http import HTTPStatus

from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, ValidationError
from ninja.errors import HttpError as NinjaHttpError
from ninja.throttling import AnonRateThrottle, AuthRateThrottle

from actions.api import router as actions_router
from ads.google.api import router as google_ads_router
from ads.google.api_ai import router as google_ads_ai_router
from ads.google.api_optimize import router as google_ads_optimize_router
from ads.google.api_practice import router as google_ads_practice_router
from ads.meta.api import router as meta_ads_router
from ads.meta.api_setup import router as meta_setup_router
from ads.openai.api import router as openai_ads_router
from ads.openai.api_ads import router as openai_ads_list_router
from ads.openai.api_store import router as openai_ads_store_router
from ads.openai.api_suggest import router as openai_ads_suggest_router
from ads.openai.api_variants import router as openai_ads_variants_router
from agents.api import router as agents_router
from audit.api import router as audit_router
from connections.api import router as connections_router
from content.api import router as websites_router
from content.api_attention import router as attention_router
from content.api_improve import router as improve_router
from content.api_insights import router as insights_router
from content.api_inventory import router as inventory_router
from content.api_pages import router as pages_router
from content.api_search import router as search_router
from content.blog.api import router as blog_router
from content.blog.api_posts import router as blog_posts_router
from content.blog.api_publish import router as blog_publish_router
from core.api.problem import problem_response
from core.errors import DomainError, ErrorCode
from core.logging import get_logger
from crm.activity.api import router as crm_activity_router
from crm.activity.appointment_api import router as crm_appointments_router
from crm.billing.api import router as crm_billing_router
from crm.campaigns.api import router as crm_campaigns_router
from crm.common.team_api import router as crm_team_router
from crm.consent.api import router as crm_consent_router
from crm.contacts.api import router as crm_contacts_router
from crm.fields.api import router as crm_fields_router
from crm.inbox.api import router as crm_inbox_router
from crm.leads.api import router as crm_leads_router
from crm.lists.api import router as crm_lists_router
from crm.meta.api import router as crm_meta_router
from crm.meta.api_public import router as crm_meta_public_router
from crm.notifications.api import router as crm_notifications_router
from crm.ops.api import router as crm_ops_router
from crm.pipelines.api import router as crm_pipelines_router
from crm.pipelines.api_reports import router as crm_pipeline_reports_router
from crm.pipelines.payments_api import router as crm_payments_router
from crm.plans.api import router as crm_plans_router
from crm.privacy.api import router as crm_privacy_router
from crm.reports.api import router as crm_reports_router
from crm.sending.api import router as crm_sending_router
from crm.sending.api_public import router as crm_public_router
from crm.sms.api import router as crm_sms_router
from crm.sms.api_public import router as crm_sms_public_router
from crm.templates.api import router as crm_templates_router
from crm.tracking.api_public import router as crm_tracking_public_router
from crm.usage.api import router as crm_usage_router
from crm.voice.api import router as crm_voice_router
from crm.voice.api_public import router as crm_voice_public_router
from crm.webhooks.api import router as crm_webhooks_router
from crm.webhooks.api_public import router as crm_webhooks_public_router
from crm.workflows.api import router as crm_workflows_router
from identity.api import router as auth_router
from media.api import router as media_router
from memory.api import router as memory_router
from social.api import router as instagram_router
from social.api_inbox import router as instagram_inbox_router
from social.api_public import router as social_public_router
from social.api_studio import router as instagram_studio_router
from studio.api import router as studio_router
from studio.api_brands import router as studio_brands_router
from studio.api_pieces import router as studio_posts_router
from studio.api_trends import router as studio_trends_router
from workspaces.api import router as workspaces_router
from workspaces.api_platform import router as platform_router

log = get_logger(__name__)

api = NinjaAPI(
    throttle=[AnonRateThrottle("60/m"), AuthRateThrottle("600/m")],
    title="Celurio Marketing API",
    version="1.0.0",
    urls_namespace="api_v1",
    docs_url="/docs",
    openapi_url="/openapi.json",
)


def _field_errors(exc: DomainError) -> list[dict[str, object]] | None:
    """Machine-readable per-field problems a domain error may carry (``errors`` or ``field``)."""
    errors = exc.params.get("errors")
    if isinstance(errors, dict):
        return [{"loc": [key], "type": str(reason)} for key, reason in errors.items()]
    if "field" in exc.params:
        return [{"loc": [str(exc.params["field"])], "type": str(exc.params.get("reason", ""))}]
    return None


@api.exception_handler(DomainError)
def handle_domain_error(request: HttpRequest, exc: DomainError) -> HttpResponse:
    if exc.http_status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        log.error("domain_error", code=exc.code.value, detail=exc.detail)
    detail = exc.detail if exc.http_status == HTTPStatus.UNPROCESSABLE_ENTITY else ""
    return problem_response(
        request,
        status=exc.http_status,
        code=exc.code,
        detail=detail,
        errors=_field_errors(exc),
    )


@api.exception_handler(ValidationError)
def handle_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    return problem_response(
        request,
        status=422,
        code=ErrorCode.VALIDATION_FAILED,
        errors=[
            {
                "loc": list(err.get("loc", [])),
                "type": err.get("type", ""),
                "msg": err.get("msg", ""),
            }
            for err in exc.errors
        ],
    )


@api.exception_handler(AuthenticationError)
def handle_auth_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    return problem_response(request, status=401, code=ErrorCode.UNAUTHENTICATED)


@api.exception_handler(NinjaHttpError)
def handle_http_error(request: HttpRequest, exc: NinjaHttpError) -> HttpResponse:
    code = {
        401: ErrorCode.UNAUTHENTICATED,
        403: ErrorCode.PERMISSION_DENIED,
        404: ErrorCode.NOT_FOUND,
        429: ErrorCode.RATE_LIMITED,
    }.get(exc.status_code, ErrorCode.UNKNOWN)
    return problem_response(request, status=exc.status_code, code=code)


@api.exception_handler(Exception)
def handle_unexpected(request: HttpRequest, exc: Exception) -> HttpResponse:
    log.exception("unhandled_exception", error=type(exc).__name__)
    return problem_response(request, status=500, code=ErrorCode.UNKNOWN)


api.add_router("/auth", auth_router)
api.add_router("/workspaces", workspaces_router)
api.add_router("/platform", platform_router)
api.add_router("/audit", audit_router)
api.add_router("/connections", connections_router)
api.add_router("/actions", actions_router)
api.add_router("/agents", agents_router)
api.add_router("/memory", memory_router)
api.add_router("/crm", crm_fields_router)
api.add_router("/crm", crm_contacts_router)
api.add_router("/crm", crm_consent_router)
api.add_router("/crm", crm_activity_router)
api.add_router("/crm", crm_appointments_router)
api.add_router("/crm", crm_team_router)
api.add_router("/crm", crm_lists_router)
api.add_router("/crm", crm_inbox_router)
api.add_router("/crm", crm_sending_router)
api.add_router("/crm", crm_templates_router)
api.add_router("/crm", crm_pipelines_router)
api.add_router("/crm", crm_payments_router)
api.add_router("/crm", crm_pipeline_reports_router)
api.add_router("/crm", crm_sms_router)
api.add_router("/crm", crm_meta_router)
api.add_router("/crm", crm_leads_router)
api.add_router("/crm", crm_workflows_router)
api.add_router("/crm", crm_reports_router)
api.add_router("/crm", crm_privacy_router)
api.add_router("/crm", crm_plans_router)
api.add_router("/crm", crm_billing_router)
api.add_router("/crm", crm_ops_router)
api.add_router("/crm", crm_voice_router)
api.add_router("/crm", crm_notifications_router)
api.add_router("/crm", crm_webhooks_router)
api.add_router("/crm", crm_campaigns_router)
api.add_router("/crm", crm_usage_router)
api.add_router("/crm-public", crm_public_router)
api.add_router("/crm-public", crm_webhooks_public_router)
api.add_router("/crm-public", crm_sms_public_router)
api.add_router("/crm-public", crm_tracking_public_router)
api.add_router("/crm-public", crm_meta_public_router)
api.add_router("/crm-public", crm_voice_public_router)
api.add_router("/media", media_router)
api.add_router("/ads/google", google_ads_router)
api.add_router("/ads/google", google_ads_ai_router)
api.add_router("/ads/google", google_ads_optimize_router)
api.add_router("/ads/google", google_ads_practice_router)
api.add_router("/meta", meta_setup_router)
api.add_router("/ads/meta", meta_ads_router)
api.add_router("/ads/openai", openai_ads_router)
api.add_router("/ads/openai", openai_ads_store_router)
api.add_router("/ads/openai", openai_ads_list_router)
api.add_router("/ads/openai", openai_ads_variants_router)
api.add_router("/ads/openai", openai_ads_suggest_router)
api.add_router("/social/instagram", instagram_router)
api.add_router("/social/instagram", instagram_inbox_router)
api.add_router("/social/instagram", instagram_studio_router)
api.add_router("/social", social_public_router)
api.add_router("/studio", studio_router)
api.add_router("/studio", studio_brands_router)
api.add_router("/studio", studio_trends_router)
api.add_router("/studio", studio_posts_router)
api.add_router("/websites", websites_router)
api.add_router("/websites", inventory_router)
api.add_router("/websites", attention_router)
api.add_router("/websites", pages_router)
api.add_router("/websites", improve_router)
api.add_router("/websites", search_router)
api.add_router("/websites", blog_router)
api.add_router("/websites", blog_posts_router)
api.add_router("/websites", blog_publish_router)
api.add_router("/websites", insights_router)
