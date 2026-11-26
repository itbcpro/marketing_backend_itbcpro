"""The Meta app keys a workspace uses for Facebook Login and the Marketing API.

Instagram Login has its own app id and secret (shown under the Instagram product of the same
Meta app), kept in ``social.instagram.credentials``. The Meta setup screen saves both.
"""

import uuid
from dataclasses import dataclass

from django.conf import settings

from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import DomainError

APP_PROVIDER = "meta_app"
FIELDS = ("app_id", "app_secret")


@dataclass(frozen=True, slots=True)
class AppCredentials:
    app_id: str
    app_secret: str

    def missing(self) -> list[str]:
        return [name for name in FIELDS if not getattr(self, name)]


def app_connection(workspace_id: uuid.UUID | None) -> Connection | None:
    if not workspace_id:
        return None
    return (
        Connection.objects.filter(workspace_id=workspace_id, provider=APP_PROVIDER)
        .exclude(status="revoked")
        .first()
    )


def app_credentials(workspace_id: uuid.UUID | None) -> AppCredentials:
    """Values stored in the workspace's "Meta app keys" connection win over settings."""
    stored = {"app_id": "", "app_secret": ""}
    connection = app_connection(workspace_id)
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
        app_id=stored["app_id"] or settings.META_APP_ID,
        app_secret=stored["app_secret"] or settings.META_APP_SECRET,
    )
