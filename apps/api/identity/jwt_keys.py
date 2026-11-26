"""Signing key ring: sign, verify with a pinned algorithm, and publish the public JWKS."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from django.conf import settings

from core.errors import ErrorCode, UnauthenticatedError

RSA_KEY_BITS = 3072
EPHEMERAL_KID = "ephemeral"
EPHEMERAL_ALLOWED_ENVIRONMENTS = frozenset({"dev", "test"})
JWK_USE = "sig"


def _generate(algorithm: str) -> bytes:
    key: ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey
    if algorithm == "EdDSA":
        key = ed25519.Ed25519PrivateKey.generate()
    elif algorithm == "ES256":
        key = ec.generate_private_key(ec.SECP256R1())
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_BITS)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class KeyRing:
    """Holds private keys by ``kid``. The algorithm is fixed by configuration, never by a token."""

    def __init__(self, pems: dict[str, bytes], active_kid: str, algorithm: str) -> None:
        if active_kid not in pems:
            raise ValueError("active JWT key id is not configured")
        self.algorithm = algorithm
        self.active_kid = active_kid
        self._private: dict[str, Any] = {
            kid: serialization.load_pem_private_key(pem, None) for kid, pem in pems.items()
        }

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims,
            self._private[self.active_kid],
            algorithm=self.algorithm,
            headers={"kid": self.active_kid},
        )

    def verify(self, token: str) -> dict[str, Any]:
        """Return verified claims or raise ``UnauthenticatedError`` (never reveals why)."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            private = self._private.get(kid) if isinstance(kid, str) else None
            if private is None:
                raise UnauthenticatedError(code=ErrorCode.INVALID_TOKEN)
            claims: dict[str, Any] = jwt.decode(
                token,
                private.public_key(),
                algorithms=[self.algorithm],
                audience=settings.JWT_AUDIENCE,
                issuer=settings.JWT_ISSUER,
                leeway=settings.JWT_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti", "sid", "ws"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError(code=ErrorCode.INVALID_TOKEN) from exc
        return claims

    def jwks(self) -> dict[str, Any]:
        algo = jwt.get_algorithm_by_name(self.algorithm)
        keys: list[dict[str, Any]] = []
        for kid, private in self._private.items():
            public = algo.prepare_key(private.public_key())
            jwk = algo.to_jwk(public, as_dict=True)
            keys.append({**jwk, "kid": kid, "use": JWK_USE, "alg": self.algorithm})
        return {"keys": keys}


@lru_cache(maxsize=1)
def get_keyring() -> KeyRing:
    pems = dict(settings.JWT_SIGNING_KEYS)
    active = settings.JWT_ACTIVE_KID
    if not pems:
        if settings.APP_ENVIRONMENT not in EPHEMERAL_ALLOWED_ENVIRONMENTS:
            raise RuntimeError("JWT signing keys are not configured")
        pems = {EPHEMERAL_KID: _generate(settings.JWT_ALGORITHM)}
        active = EPHEMERAL_KID
    return KeyRing(pems, active, settings.JWT_ALGORITHM)


def reset_keyring() -> None:
    get_keyring.cache_clear()
