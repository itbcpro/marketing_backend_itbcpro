import base64
from datetime import UTC, datetime

import pytest

from config.env import Settings
from core.clock import FrozenClock, SystemClock
from core.locale import negotiate_locale


def _key() -> str:
    return base64.b64encode(b"k" * 32).decode()


def test_prod_requires_secret_key_and_vault_keys() -> None:
    with pytest.raises(ValueError, match="APP_SECRET_KEY must be set"):
        Settings(environment="prod", csrf_trusted_origins=["https://x.test"])


def test_prod_rejects_debug_and_short_secret() -> None:
    with pytest.raises(ValueError, match="at least 50"):
        Settings(
            environment="prod",
            secret_key="short",
            vault_master_keys=f"v1:{_key()}",
            csrf_trusted_origins=["https://x.test"],
        )
    with pytest.raises(ValueError, match="APP_DEBUG must be false"):
        Settings(
            environment="prod",
            secret_key="x" * 60,
            vault_master_keys=f"v1:{_key()}",
            csrf_trusted_origins=["https://x.test"],
            debug=True,
        )


def _jwt_key() -> str:
    from identity.jwt_keys import _generate

    return base64.b64encode(_generate("EdDSA")).decode()


def test_prod_requires_jwt_signing_keys() -> None:
    with pytest.raises(ValueError, match="APP_JWT_SIGNING_KEYS"):
        Settings(
            environment="prod",
            secret_key="x" * 60,
            vault_master_keys=f"v1:{_key()}",
            csrf_trusted_origins=["https://x.test"],
        )


def test_active_jwt_kid_must_exist() -> None:
    with pytest.raises(ValueError, match="APP_JWT_ACTIVE_KID"):
        Settings(jwt_signing_keys=f"k1:{_jwt_key()}", jwt_active_kid="missing")


def test_valid_prod_settings_parse_vault_keys() -> None:
    settings = Settings(
        environment="prod",
        secret_key="x" * 60,
        vault_master_keys=f"v1:{_key()}",
        csrf_trusted_origins=["https://x.test"],
        jwt_signing_keys=f"k1:{_jwt_key()}",
        jwt_active_kid="k1",
    )
    assert set(settings.parsed_vault_keys()) == {"v1"}
    assert set(settings.parsed_jwt_keys()) == {"k1"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("nocolon", "version:base64key"),
        ("v1:!!!notbase64", "not valid base64"),
        (f"v1:{base64.b64encode(b'short').decode()}", "exactly 32 bytes"),
    ],
)
def test_bad_vault_keys_are_reported(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(vault_master_keys=value)


def test_active_key_version_must_exist() -> None:
    with pytest.raises(ValueError, match="ACTIVE_KEY_VERSION"):
        Settings(vault_master_keys=f"v1:{_key()}", vault_active_key_version="v9")


def test_default_locale_must_be_supported() -> None:
    with pytest.raises(ValueError, match="DEFAULT_LOCALE"):
        Settings(default_locale="de")


def test_negotiate_locale_falls_back_to_default() -> None:
    supported = ["en", "fr"]
    assert negotiate_locale("fr-CA", supported, "en") == "fr"
    assert negotiate_locale("de", supported, "en") == "en"
    assert negotiate_locale(None, supported, "en") == "en"


def test_clocks() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FrozenClock(moment)
    assert clock.now() == moment
    clock.set(datetime(2027, 1, 1, tzinfo=UTC))
    assert clock.now().year == 2027
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 1, 1))  # noqa: DTZ001
    assert SystemClock().now().tzinfo is not None
