"""Print the cryptographic parts of the production env file (run it, redirect to a file outside git).

    cd apps/api && uv run python ../../infra/deploy/generate_env.py > deploy.env

Every value is random and printed once; keep the file only long enough to paste it into the
DEPLOY_ENV secret, then delete it.
"""

import base64
import os
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    private = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    lines = {
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "REDIS_PASSWORD": secrets.token_hex(32),
        "APP_SECRET_KEY": secrets.token_urlsafe(64),
        "APP_VAULT_MASTER_KEYS": "v1:" + base64.b64encode(os.urandom(32)).decode(),
        "APP_JWT_SIGNING_KEYS": "k1:" + base64.b64encode(private).decode(),
    }
    for key, value in lines.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
