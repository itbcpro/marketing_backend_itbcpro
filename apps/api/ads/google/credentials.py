"""The Google app credentials a workspace uses: its own connection first, then platform settings."""

import uuid
from dataclasses import dataclass

from django.conf import settings

from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import DomainError

APP_PROVIDER = "google_ads_app"
FIELDS = ("client_id", "client_secret")  # the developer token is optional since 2026


@dataclass(frozen=True, slots=True)
class AppCredentials:
    client_id: str
    client_secret: str
    developer_token: str

    def missing(self) -> list[str]:
        return [name for name in FIELDS if not getattr(self, name)]


def app_credentials(workspace_id: uuid.UUID | None) -> AppCredentials:
    """Values stored in the workspace's "Google Ads app keys" connection win over settings."""
    stored = {"client_id": "", "client_secret": "", "developer_token": ""}
    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider=APP_PROVIDER)
        .exclude(status="revoked")
        .first()
        if workspace_id
        else None
    )
    if connection is not None:
        for name, kind in (
            ("client_id", CredentialKind.API_KEY),
            ("client_secret", CredentialKind.CLIENT_SECRET),
            ("developer_token", CredentialKind.DEVELOPER_TOKEN),
        ):
            try:
                stored[name] = read_secret(connection, kind)
            except DomainError:
                stored[name] = ""
    return AppCredentials(
        client_id=stored["client_id"] or settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=stored["client_secret"] or settings.GOOGLE_OAUTH_CLIENT_SECRET,
        developer_token=stored["developer_token"] or settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )
