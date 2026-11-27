"""Where Google and Microsoft send the browser back after a person signs in."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from ninja import Router
from ninja.throttling import BaseThrottle

from audit.actors import Actor
from core import tenancy
from core.errors import ConflictError, ValidationFailedError
from crm.common.throttles import PublicLinkThrottle
from crm.mailboxes import oauth, services

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [PublicLinkThrottle("60/m")]


def _back(**query: str) -> HttpResponse:
    """Always to the configured page: the address in the request is never followed."""
    target = settings.CRM_MAILBOX_RETURN_URL or "/"
    return HttpResponseRedirect(f"{target}?{urlencode(query)}")


@router.get(
    "/mailboxes/oauth/callback",
    response={302: None},
    throttle=THROTTLE,
    operation_id="crm_public_mailbox_callback",
    include_in_schema=False,
)
def callback(
    request: HttpRequest, state: str = "", code: str = "", error: str = ""
) -> HttpResponse:
    if error or not code or not state:
        return _back(mailbox_error="denied" if error else "invalid_request")
    try:
        grant = oauth.finish(state=state, code=code)
        with tenancy.use_workspace(grant.workspace_id):
            services.connect_oauth(grant=grant, actor=Actor.human(grant.user_id))
    except oauth.OAuthError as exc:
        return _back(mailbox_error=exc.code)
    except (ConflictError, ValidationFailedError) as exc:
        problems = cast("dict[str, str]", exc.params.get("errors") or {})
        return _back(mailbox_error=str(next(iter(problems.values()), "failed")))
    return _back(mailbox="connected")
