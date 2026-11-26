"""Test settings: fast hashing, in-memory cache, eager tasks."""

import base64
import os

os.environ["APP_ENVIRONMENT"] = "test"
os.environ.setdefault(
    "APP_VAULT_MASTER_KEYS",
    "v1:" + base64.b64encode(b"0" * 32).decode() + ",v2:" + base64.b64encode(b"1" * 32).decode(),
)
os.environ.setdefault("APP_VAULT_ACTIVE_KEY_VERSION", "v1")

from config.settings.base import *

MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
MFA_REQUIRED_ROLES = []
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
DATABASES["default"]["CONN_MAX_AGE"] = 0
DATABASES["default"]["OPTIONS"] = {}
SECURE_CSP_REPORT_ONLY = None
