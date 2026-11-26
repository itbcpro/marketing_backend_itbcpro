"""Short-lived public links to media files.

Instagram downloads the image or video of a post from a public HTTPS address, but files are private
to their workspace. A link carries a signed, expiring token for exactly one asset; nothing else is
exposed and nobody can guess another file's link.
"""

import uuid

from django.conf import settings
from django.core import signing

from core.errors import NotFoundError, ValidationFailedError
from media.models import MediaAsset

SALT = "social.public-media"
MAX_AGE_SECONDS = 6 * 3600
PATH = "/bff/api/v1/social/public-media"


def sign_asset(asset_id: uuid.UUID) -> str:
    return signing.dumps(str(asset_id), salt=SALT, compress=False)


def resolve_token(token: str) -> MediaAsset:
    """The asset a valid, unexpired token points to; anything else is simply not found."""
    try:
        raw = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
        asset = MediaAsset.objects.filter(pk=uuid.UUID(str(raw))).first()
    except (signing.BadSignature, ValueError) as exc:
        raise NotFoundError("link is not valid") from exc
    if asset is None or not asset.storage_key:
        raise NotFoundError("link is not valid")
    return asset


def public_url(asset: MediaAsset) -> str:
    """A link Instagram can fetch. Requires a public HTTPS origin."""
    base = str(settings.PUBLIC_BASE_URL).rstrip("/")
    if not base.startswith("https://"):
        raise ValidationFailedError(
            "set APP_PUBLIC_BASE_URL to the public https address of this site; "
            "Instagram cannot download media from a private or http address"
        )
    return f"{base}{PATH}/{sign_asset(asset.pk)}"
