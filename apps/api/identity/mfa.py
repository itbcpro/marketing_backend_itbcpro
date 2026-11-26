"""TOTP enrolment/verification and single-use recovery codes."""

import hashlib
import hmac
import secrets
import time

import pyotp
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core import vault
from core.errors import ConflictError, ErrorCode, ValidationFailedError
from identity.models import PasskeyCredential, RecoveryCode, TotpDevice, User

TOTP_PERIOD = 30
RECOVERY_CODE_COUNT = 10
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _aad(user: User) -> bytes:
    return f"totp:{user.pk}".encode()


def _hash_code(code: str) -> str:
    normalised = code.replace("-", "").replace(" ", "").upper()
    return hashlib.sha256(normalised.encode()).hexdigest()


def _sealed(device: TotpDevice) -> vault.SealedSecret:
    return vault.SealedSecret(
        ciphertext=bytes(device.ciphertext),
        nonce=bytes(device.nonce),
        wrapped_key=bytes(device.wrapped_key),
        wrapped_key_nonce=bytes(device.wrapped_key_nonce),
        key_version=device.key_version,
    )


def _secret_of(device: TotpDevice) -> str:
    return vault.open_secret(_sealed(device), aad=_aad(device.user)).decode()


def has_confirmed_totp(user: User) -> bool:
    return TotpDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def has_passkey(user: User) -> bool:
    return PasskeyCredential.objects.filter(user=user).exists()


def mfa_enabled(user: User) -> bool:
    return has_confirmed_totp(user) or has_passkey(user)


def begin_totp_enrollment(user: User) -> tuple[str, str]:
    """Create (or replace) an unconfirmed device. Returns the base32 secret and otpauth URI."""
    if has_confirmed_totp(user):
        raise ConflictError("totp already enrolled")
    secret = pyotp.random_base32()
    sealed = vault.seal(secret.encode(), aad=_aad(user))
    TotpDevice.objects.update_or_create(
        user=user,
        defaults={
            "ciphertext": sealed.ciphertext,
            "nonce": sealed.nonce,
            "wrapped_key": sealed.wrapped_key,
            "wrapped_key_nonce": sealed.wrapped_key_nonce,
            "key_version": sealed.key_version,
            "confirmed_at": None,
            "last_used_step": 0,
        },
    )
    uri = pyotp.TOTP(secret, interval=TOTP_PERIOD).provisioning_uri(
        name=user.email, issuer_name=settings.WEBAUTHN_RP_NAME
    )
    return secret, uri


def _verify_step(device: TotpDevice, code: str) -> int | None:
    """Return the accepted time step, or ``None``. Steps already used are rejected (no replay)."""
    if not (code.isdigit() and len(code) == 6):  # noqa: PLR2004 - standard TOTP length
        return None
    totp = pyotp.TOTP(_secret_of(device), interval=TOTP_PERIOD)
    current = int(time.time() // TOTP_PERIOD)
    for step in (current - 1, current, current + 1):
        expected = totp.at(step * TOTP_PERIOD)
        if hmac.compare_digest(expected, code) and step > device.last_used_step:
            return step
    return None


def confirm_totp_enrollment(user: User, code: str) -> list[str]:
    device = TotpDevice.objects.filter(user=user, confirmed_at__isnull=True).first()
    if device is None:
        raise ValidationFailedError(code=ErrorCode.INVALID_MFA_CODE)
    step = _verify_step(device, code)
    if step is None:
        raise ValidationFailedError(code=ErrorCode.INVALID_MFA_CODE)
    with transaction.atomic():
        device.confirmed_at = timezone.now()
        device.last_used_step = step
        device.save(update_fields=["confirmed_at", "last_used_step", "updated_at"])
        codes = generate_recovery_codes(user)
        record_event(
            action="mfa.totp_enrolled",
            actor=Actor.human(user.pk),
            target_type="user",
            target_id=user.pk,
        )
    return codes


def verify_totp(user: User, code: str) -> bool:
    device = TotpDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    if device is None:
        return False
    step = _verify_step(device, code.strip())
    if step is None:
        return False
    device.last_used_step = step
    device.save(update_fields=["last_used_step", "updated_at"])
    return True


def disable_totp(user: User) -> None:
    with transaction.atomic():
        TotpDevice.objects.filter(user=user).delete()
        RecoveryCode.objects.filter(user=user).delete()
        record_event(
            action="mfa.totp_disabled",
            actor=Actor.human(user.pk),
            target_type="user",
            target_id=user.pk,
        )


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def generate_recovery_codes(user: User) -> list[str]:
    """Replace all recovery codes; the plaintext values are returned exactly once."""
    with transaction.atomic():
        RecoveryCode.objects.filter(user=user).delete()
        codes = [_new_code() for _ in range(RECOVERY_CODE_COUNT)]
        RecoveryCode.objects.bulk_create(
            [RecoveryCode(user=user, code_hash=_hash_code(code)) for code in codes]
        )
    return codes


def use_recovery_code(user: User, code: str) -> bool:
    matched = RecoveryCode.objects.filter(
        user=user, code_hash=_hash_code(code), used_at__isnull=True
    ).update(used_at=timezone.now())
    if matched:
        record_event(
            action="mfa.recovery_code_used",
            actor=Actor.human(user.pk),
            target_type="user",
            target_id=user.pk,
        )
    return bool(matched)


def remaining_recovery_codes(user: User) -> int:
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


def requires_mfa_enrollment(user: User) -> bool:
    """True when the user must use MFA and none is set up yet.

    Platform administrators (superusers) always must: they can create tenants and owners.
    """
    if user.is_superuser:
        return not mfa_enabled(user)
    required = list(settings.MFA_REQUIRED_ROLES)
    if not required or mfa_enabled(user):
        return False
    return user.memberships.filter(role__in=required).exists()
