"""Platform panel: register the Google and Microsoft apps that mailbox sign-in uses.

Reachable only by a platform administrator signed in with a session that completed MFA; everyone
else gets "not found". Secrets can be set or cleared here but are never returned.
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Schema

from core.errors import NotFoundError
from crm.mailboxes import platform_config
from identity.authentication import EnrolledSessionAuth
from identity.models import User

router = Router(tags=["platform"], auth=EnrolledSessionAuth())


class ProviderOut(Schema):
    client_id: str
    secret_set: bool
    source: str


class MailboxOAuthOut(Schema):
    google: ProviderOut
    microsoft: ProviderOut
    microsoft_tenant: str
    redirect_uri: str
    return_url: str
    # What is actually in force, including values that come from the environment.
    effective_redirect_uri: str
    effective_return_url: str


class MailboxOAuthIn(Schema):
    """Fields left out are unchanged. A secret of "" clears it; a text field of "" falls back."""

    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant: str | None = None
    redirect_uri: str | None = None
    return_url: str | None = None


def _admin(request: HttpRequest) -> User:
    user = request.auth  # type: ignore[attr-defined]
    if not isinstance(user, User) or not (user.is_superuser and user.is_active):
        raise NotFoundError("not found")
    return user


def _out() -> MailboxOAuthOut:
    stored, now = platform_config.stored_values(), platform_config.current()
    return MailboxOAuthOut(
        google=ProviderOut(
            client_id=now.google_client_id,
            secret_set=platform_config.secret_is_set("google"),
            source=platform_config.source_of("google"),
        ),
        microsoft=ProviderOut(
            client_id=now.microsoft_client_id,
            secret_set=platform_config.secret_is_set("microsoft"),
            source=platform_config.source_of("microsoft"),
        ),
        microsoft_tenant=stored["microsoft_tenant"],
        redirect_uri=stored["redirect_uri"],
        return_url=stored["return_url"],
        effective_redirect_uri=now.redirect_uri,
        effective_return_url=now.return_url,
    )


@router.get("/mailbox-oauth", response=MailboxOAuthOut, operation_id="platform_mailbox_oauth")
def get_settings(request: HttpRequest) -> MailboxOAuthOut:
    _admin(request)
    return _out()


@router.put("/mailbox-oauth", response=MailboxOAuthOut, operation_id="platform_mailbox_oauth_save")
def save_settings(request: HttpRequest, payload: MailboxOAuthIn) -> MailboxOAuthOut:
    admin = _admin(request)
    platform_config.save(admin_id=admin.pk, changes=payload.model_dump(exclude_unset=True))
    return _out()
