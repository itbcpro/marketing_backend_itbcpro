"""The Google OAuth app for Search Console: own keys, then the Google Ads keys, then settings."""

import uuid
from dataclasses import dataclass

from django.conf import settings

from ads.google.credentials import APP_PROVIDER as ADS_APP_PROVIDER
from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import DomainError

APP_PROVIDER = "google_search_app"
FIELDS = ("client_id", "client_secret")


@dataclass(frozen=True, slots=True)
class AppCredentials:
    client_id: str
    client_secret: str

    def missing(self) -> list[str]:
        return [name for name in FIELDS if not getattr(self, name)]


def _stored(workspace_id: uuid.UUID | None, provider: str) -> dict[str, str]:
    found = {"client_id": "", "client_secret": ""}
    if workspace_id is None:
        return found
    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider=provider)
        .exclude(status="revoked")
        .first()
    )
    if connection is None:
        return found
    for name, kind in (
        ("client_id", CredentialKind.API_KEY),
        ("client_secret", CredentialKind.CLIENT_SECRET),
    ):
        try:
            found[name] = read_secret(connection, kind)
        except DomainError:
            found[name] = ""
    return found


def app_credentials(workspace_id: uuid.UUID | None) -> AppCredentials:
    """Search Console keys win; the Google Ads app keys (same kind of Google client) are reused."""
    own = _stored(workspace_id, APP_PROVIDER)
    ads = _stored(workspace_id, ADS_APP_PROVIDER)
    return AppCredentials(
        client_id=own["client_id"] or ads["client_id"] or settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=own["client_secret"]
        or ads["client_secret"]
        or settings.GOOGLE_OAUTH_CLIENT_SECRET,
    )
