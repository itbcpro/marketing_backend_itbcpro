"""The Instagram app keys a workspace uses: its own connection first, then platform settings."""

import uuid
from dataclasses import dataclass

from django.conf import settings

from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import DomainError

APP_PROVIDER = "instagram_app"
FIELDS = ("app_id", "app_secret")


@dataclass(frozen=True, slots=True)
class AppCredentials:
    app_id: str
    app_secret: str

    def missing(self) -> list[str]:
        return [name for name in FIELDS if not getattr(self, name)]


def app_credentials(workspace_id: uuid.UUID | None) -> AppCredentials:
    """Values stored in the workspace's "Instagram app keys" connection win over settings."""
    stored = {"app_id": "", "app_secret": ""}
    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider=APP_PROVIDER)
        .exclude(status="revoked")
        .first()
        if workspace_id
        else None
    )
    if connection is not None:
        for name, kind in (
            ("app_id", CredentialKind.API_KEY),
            ("app_secret", CredentialKind.CLIENT_SECRET),
        ):
            try:
                stored[name] = read_secret(connection, kind)
            except DomainError:
                stored[name] = ""
    return AppCredentials(
        app_id=stored["app_id"] or settings.INSTAGRAM_APP_ID,
        app_secret=stored["app_secret"] or settings.INSTAGRAM_APP_SECRET,
    )
