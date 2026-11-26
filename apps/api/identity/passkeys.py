"""WebAuthn passkeys used as a phishing-resistant second factor.

Challenges are kept in the server-side session, never in the browser.
"""

import base64
import json
import uuid
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from audit.actors import Actor
from audit.services import record_event
from core.errors import ErrorCode, NotFoundError, ValidationFailedError
from identity.models import PasskeyCredential, User

REGISTRATION_CHALLENGE_KEY = "webauthn_registration_challenge"
AUTHENTICATION_CHALLENGE_KEY = "webauthn_authentication_challenge"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _descriptors(user: User) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(id=bytes(p.credential_id))
        for p in PasskeyCredential.objects.filter(user=user)
    ]


def begin_registration(request: HttpRequest, user: User) -> dict[str, Any]:
    options = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_id=user.pk.bytes,
        user_name=user.email,
        exclude_credentials=_descriptors(user),
    )
    request.session[REGISTRATION_CHALLENGE_KEY] = _b64(options.challenge)
    parsed: dict[str, Any] = json.loads(options_to_json(options))
    return parsed


def finish_registration(
    request: HttpRequest, user: User, credential: dict[str, Any], name: str
) -> PasskeyCredential:
    challenge = request.session.pop(REGISTRATION_CHALLENGE_KEY, None)
    if not challenge:
        raise ValidationFailedError(code=ErrorCode.INVALID_TOKEN)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=_unb64(challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGIN,
        )
    except Exception as exc:
        raise ValidationFailedError(code=ErrorCode.INVALID_MFA_CODE) from exc
    passkey = PasskeyCredential.objects.create(
        user=user,
        name=name[:100],
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=list(credential.get("response", {}).get("transports", [])),
    )
    record_event(
        action="mfa.passkey_registered",
        actor=Actor.human(user.pk),
        target_type="user",
        target_id=user.pk,
        after={"name": passkey.name},
    )
    return passkey


def begin_authentication(request: HttpRequest, user: User) -> dict[str, Any]:
    descriptors = _descriptors(user)
    if not descriptors:
        raise NotFoundError("no passkeys registered")
    options = generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID, allow_credentials=descriptors
    )
    request.session[AUTHENTICATION_CHALLENGE_KEY] = _b64(options.challenge)
    parsed: dict[str, Any] = json.loads(options_to_json(options))
    return parsed


def verify_authentication(request: HttpRequest, user: User, credential: dict[str, Any]) -> bool:
    challenge = request.session.pop(AUTHENTICATION_CHALLENGE_KEY, None)
    raw_id = credential.get("rawId") or credential.get("id")
    if not challenge or not raw_id:
        return False
    stored = PasskeyCredential.objects.filter(
        user=user, credential_id=base64url_to_bytes(str(raw_id))
    ).first()
    if stored is None:
        return False
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=_unb64(challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGIN,
            credential_public_key=bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
        )
    except Exception:
        return False
    stored.sign_count = verified.new_sign_count
    stored.last_used_at = timezone.now()
    stored.save(update_fields=["sign_count", "last_used_at", "updated_at"])
    return True


def remove_passkey(user: User, passkey_id: uuid.UUID) -> None:
    deleted, _ = PasskeyCredential.objects.filter(pk=passkey_id, user=user).delete()
    if not deleted:
        raise NotFoundError("passkey not found")
    record_event(
        action="mfa.passkey_removed",
        actor=Actor.human(user.pk),
        target_type="user",
        target_id=user.pk,
    )
