"""Development settings."""

import os

os.environ.setdefault("APP_ENVIRONMENT", "dev")
os.environ.setdefault("APP_DEBUG", "true")
os.environ.setdefault("APP_ADMIN_ENABLED", "true")
os.environ.setdefault("APP_WEB_BASE_URL", "http://localhost:3003")
# Development-only vault key so secrets (MFA, connections) work without extra setup.
# Never used outside dev; staging and production must set APP_VAULT_MASTER_KEYS themselves.
os.environ.setdefault("APP_VAULT_MASTER_KEYS", "v1:4eKrtddq4WU5+abmHxzzA2loshJiP8ohfG3gSimsPNQ=")

from config.settings.base import *

ALLOWED_HOSTS = ["*"]

# The web app runs on whichever local port is free (3000 is often taken), so trust them all in dev.
CSRF_TRUSTED_ORIGINS = [
    *CSRF_TRUSTED_ORIGINS,
    *(
        f"http://{host}:{port}"
        for host in ("localhost", "127.0.0.1")
        for port in (3000, 3002, 3003, 3100)
    ),
]
