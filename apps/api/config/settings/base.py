"""Base Django settings shared by every environment."""

from pathlib import Path
from urllib.parse import unquote, urlparse

from django.utils.csp import CSP

from config.env import get_settings

env = get_settings()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = BASE_DIR.parent.parent

SECRET_KEY = env.secret_key.get_secret_value()
DEBUG = env.debug
ALLOWED_HOSTS = env.allowed_hosts
CSRF_TRUSTED_ORIGINS = env.csrf_trusted_origins
APP_ENVIRONMENT = env.environment
ADMIN_ENABLED = env.admin_enabled

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "django_celery_beat",
    "ninja",
    "core",
    "identity",
    "workspaces",
    "connections",
    "audit",
    "notifications",
    "llm",
    "agents",
    "memory",
    "actions",
    "ads",
    "social",
    "studio",
    "content",
    "intel",
    "inbound",
    "reporting",
    "media",
    "crm.events",
    "crm.fields",
    "crm.contacts",
    "crm.consent",
    "crm.activity",
    "crm.lists",
    "crm.inbox",
    "crm.sending",
    "crm.templates",
    "crm.pipelines",
    "crm.webhooks",
    "crm.mailboxes",
    "crm.notifications",
    "crm.sms",
    "crm.voice",
    "crm.meta",
    "crm.leads",
    "crm.workflows",
    "crm.reports",
    "crm.privacy",
    "crm.ops",
    "crm.plans",
    "crm.billing",
    "crm.tracking",
    "crm.campaigns",
    "crm.usage",
]
if ADMIN_ENABLED:
    INSTALLED_APPS.insert(0, "django.contrib.admin")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.RequestIdMiddleware",
    "core.middleware.BodySizeLimitMiddleware",
    "core.metrics.MetricsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "identity.sessions.SessionActivityMiddleware",
    "core.middleware.IdempotencyMiddleware",
    "core.middleware.TenantContextMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


def _database_from_url(url: str) -> dict[str, object]:
    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "options": (
                "-c statement_timeout=30000 -c lock_timeout=10000 "
                "-c idle_in_transaction_session_timeout=60000"
            ),
        },
    }


DATABASES = {"default": _database_from_url(env.database_url)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "identity.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
if env.password_breach_check:
    AUTH_PASSWORD_VALIDATORS.append({"NAME": "identity.validators.BreachedPasswordValidator"})

LANGUAGE_CODE = env.default_locale
LANGUAGES = [(code, code) for code in env.supported_locales]
LOCALE_PATHS = [REPO_ROOT / "locale"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = env.time_zone

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SECURE_CSP = {
    "default-src": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "form-action": [CSP.NONE],
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.redis_url,
    }
}

CELERY_BROKER_URL = env.redis_url
CELERY_RESULT_BACKEND = None
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TIME_LIMIT = 900
CELERY_TASK_SOFT_TIME_LIMIT = 840
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_DEFAULT_QUEUE = "default"

NINJA_NUM_PROXIES = 1

PROVIDER_API_VERSIONS = env.provider_api_versions
MODEL_ROUTING = env.model_routing
EFFORT_BY_TIER = env.effort_by_tier
ANTHROPIC_API_KEY = env.anthropic_api_key.get_secret_value()
LLM_DAILY_BUDGET_MICROS = env.llm_daily_budget_micros
PROMPTS_DIR = REPO_ROOT / "prompts"
VAULT_MASTER_KEYS = env.parsed_vault_keys()
VAULT_ACTIVE_KEY_VERSION = env.vault_active_key_version
DB_RUNTIME_ROLE = env.db_runtime_role
CRM_DEFAULT_PHONE_REGION = env.crm_default_phone_region
CRM_TEMPLATE_FALLBACK = env.crm_template_fallback
CRM_WALLET_CURRENCY = env.crm_wallet_currency
CRM_CAMPAIGN_BATCH_SIZE = env.crm_campaign_batch_size
CRM_CAMPAIGN_BATCH_INTERVAL = env.crm_campaign_batch_interval
CRM_CAMPAIGN_DEFER_SECONDS = env.crm_campaign_defer_seconds
CRM_EMAIL_DNS_PROFILE = env.crm_email_dns_profile
CRM_DNS_TIMEOUT_SECONDS = env.crm_dns_timeout_seconds
CRM_GOOGLE_CLIENT_ID = env.crm_google_client_id
CRM_GOOGLE_CLIENT_SECRET = env.crm_google_client_secret.get_secret_value()
CRM_MICROSOFT_CLIENT_ID = env.crm_microsoft_client_id
CRM_MICROSOFT_CLIENT_SECRET = env.crm_microsoft_client_secret.get_secret_value()
CRM_MICROSOFT_TENANT = env.crm_microsoft_tenant
CRM_MAILBOX_REDIRECT_URI = env.crm_mailbox_redirect_uri
CRM_MAILBOX_RETURN_URL = env.crm_mailbox_return_url
CRM_MAILBOX_ALLOW_PRIVATE_HOSTS = env.crm_mailbox_allow_private_hosts
CRM_MAILBOX_DEFAULT_LIMITS = env.crm_mailbox_default_limits
CRM_MAILBOX_OAUTH_TTL_SECONDS = env.crm_mailbox_oauth_ttl_seconds
CRM_MAILBOX_SYNC_DAYS = env.crm_mailbox_sync_days
CRM_MAILBOX_SYNC_BATCH = env.crm_mailbox_sync_batch
CRM_INBOX_RECENT_DAYS = env.crm_inbox_recent_days
CRM_INBOX_BULK_LIMIT = env.crm_inbox_bulk_limit
CRM_CONTACTS_BULK_LIMIT = env.crm_contacts_bulk_limit
CRM_UNSUBSCRIBE_MAX_AGE_DAYS = env.crm_unsubscribe_max_age_days
CRM_SEND_PER_MINUTE = env.crm_send_per_minute
CRM_DELIVER_MAX_RETRIES = env.crm_deliver_max_retries
CRM_FILTER_MAX_CONDITIONS = env.crm_filter_max_conditions
CRM_EXPORT_MAX_ROWS = env.crm_export_max_rows
CRM_BULK_MAX_RECORDS = env.crm_bulk_max_records
CRM_BULK_CHUNK_SIZE = env.crm_bulk_chunk_size
CRM_IMPORT_MAX_BYTES = env.crm_import_max_bytes
CRM_IMPORT_MAX_ROWS = env.crm_import_max_rows
CRM_IMPORT_ERROR_LIMIT = env.crm_import_error_limit
CRM_DEFAULT_LAW_PROFILE = env.crm_default_law_profile
CRM_MERGE_UNDO_HOURS = env.crm_merge_undo_hours
CRM_STALE_DEAL_DAYS = env.crm_stale_deal_days
CRM_INSIGHTS_DAYS = env.crm_insights_days
CRM_DIGEST_MIN_AGE_MINUTES = env.crm_digest_min_age_minutes
CRM_TWILIO_API_BASE = env.crm_twilio_api_base
CRM_SMS_TIMEOUT_SECONDS = env.crm_sms_timeout_seconds
CRM_VOICE_RING_SECONDS = env.crm_voice_ring_seconds
CRM_TRACKING_MAX_AGE_DAYS = env.crm_tracking_max_age_days
CRM_TRACKING_RETENTION_DAYS = env.crm_tracking_retention_days
CRM_HEALTH_OUTBOX_LAG_SECONDS = env.crm_health_outbox_lag_seconds
CRM_HEALTH_OVERDUE_RUNS = env.crm_health_overdue_runs
CRM_HEALTH_OVERDUE_AFTER_SECONDS = env.crm_health_overdue_after_seconds
CRM_HEALTH_FAILURE_RATE = env.crm_health_failure_rate
CRM_HEALTH_MIN_MESSAGES = env.crm_health_min_messages
CRM_WEBHOOK_RETENTION_DAYS = env.crm_webhook_retention_days
CRM_PROCESSED_EVENT_RETENTION_DAYS = env.crm_processed_event_retention_days
CRM_WORKFLOW_RETENTION_DAYS = env.crm_workflow_retention_days
CRM_NOTIFICATION_RETENTION_DAYS = env.crm_notification_retention_days
CRM_RECORDING_RETENTION_DAYS = env.crm_recording_retention_days
CRM_REPORT_DEFAULT_DAYS = env.crm_report_default_days
CRM_REPORT_MAX_DAYS = env.crm_report_max_days
CRM_WORKFLOW_MAX_STEPS = env.crm_workflow_max_steps
CRM_WORKFLOW_MAX_WAIT_DAYS = env.crm_workflow_max_wait_days
CRM_WORKFLOW_RUN_STEP_LIMIT = env.crm_workflow_run_step_limit
CRM_WORKFLOW_MAX_ATTEMPTS = env.crm_workflow_max_attempts
CRM_WORKFLOW_RETRY_BASE_SECONDS = env.crm_workflow_retry_base_seconds
CRM_WORKFLOW_BLOCKED_RETRY_MINUTES = env.crm_workflow_blocked_retry_minutes
CRM_WORKFLOW_LOST_AFTER_SECONDS = env.crm_workflow_lost_after_seconds
CRM_META_GRAPH_BASE = env.crm_meta_graph_base
CRM_META_TIMEOUT_SECONDS = env.crm_meta_timeout_seconds
CRM_VOICEMAIL_MAX_SECONDS = env.crm_voicemail_max_seconds
CRM_WEBHOOK_TOLERANCE_SECONDS = env.crm_webhook_tolerance_seconds
CRM_WEBHOOK_MAX_BODY_BYTES = env.crm_webhook_max_body_bytes
CRM_RESTORE_DAYS = env.crm_restore_days
CRM_MAX_TAGS_PER_REQUEST = env.crm_max_tags_per_request
JWT_ISSUER = env.jwt_issuer
JWT_AUDIENCE = env.jwt_audience
JWT_ALGORITHM = env.jwt_algorithm
JWT_SIGNING_KEYS = env.parsed_jwt_keys()
JWT_ACTIVE_KID = env.jwt_active_kid
JWT_ACCESS_TTL_SECONDS = env.jwt_access_ttl_seconds
JWT_REFRESH_TTL_DAYS = env.jwt_refresh_ttl_days
JWT_REFRESH_IDLE_DAYS = env.jwt_refresh_idle_days
JWT_LEEWAY_SECONDS = env.jwt_leeway_seconds
SENTRY_DSN = env.sentry_dsn.get_secret_value()
LOG_LEVEL = env.log_level
METRICS_TOKEN = env.metrics_token.get_secret_value()
OTEL_ENDPOINT = env.otel_endpoint
OTEL_SERVICE_NAME = env.otel_service_name
API_MAX_BODY_BYTES = env.max_body_bytes
# Upload endpoints accept large bodies (video files); everything else stays small.
API_UPLOAD_PATHS = ("/api/v1/media/upload",)
API_MAX_UPLOAD_BYTES = env.media_max_upload_bytes
GOOGLE_OAUTH_CLIENT_ID = env.google_oauth_client_id
GOOGLE_OAUTH_CLIENT_SECRET = env.google_oauth_client_secret.get_secret_value()
GOOGLE_ADS_DEVELOPER_TOKEN = env.google_ads_developer_token.get_secret_value()
GOOGLE_PAGESPEED_API_KEY = env.google_pagespeed_api_key.get_secret_value()
META_APP_ID = env.meta_app_id
META_APP_SECRET = env.meta_app_secret.get_secret_value()
INSTAGRAM_APP_ID = env.instagram_app_id
INSTAGRAM_APP_SECRET = env.instagram_app_secret.get_secret_value()
INSTAGRAM_WEBHOOK_VERIFY_TOKEN = env.instagram_webhook_verify_token.get_secret_value()
# Public HTTPS origin of the web app. Instagram downloads media from it, so it must be reachable
# from the internet (a tunnel in development); empty falls back to WEB_BASE_URL.
PUBLIC_BASE_URL = env.public_base_url or env.web_base_url
SIGNUP_ENABLED = env.signup_enabled
IMAGE_MODEL = env.image_model
IMAGE_QUALITY = env.image_quality
MEDIA_ROOT = env.media_root or str(REPO_ROOT / ".media")
MEDIA_MAX_IMAGE_BYTES = env.media_max_image_bytes
DATA_UPLOAD_MAX_MEMORY_SIZE = env.max_body_bytes
EMAIL_BACKEND = env.email_backend
DEFAULT_FROM_EMAIL = env.default_from_email
WEB_BASE_URL = env.web_base_url
AUDIT_RETENTION_DAYS = env.audit_retention_days
PROVIDER_SUNSET_DATES = env.provider_sunset_dates
PROVIDER_DOC_URLS = env.provider_doc_urls
MFA_REQUIRED_ROLES = env.mfa_required_roles
LOGIN_MAX_FAILURES = env.login_max_failures
LOGIN_LOCK_MINUTES = env.login_lock_minutes
WEBAUTHN_RP_ID = env.webauthn_rp_id
WEBAUTHN_RP_NAME = env.webauthn_rp_name
WEBAUTHN_ORIGIN = env.web_base_url
PASSWORD_RESET_TIMEOUT = 3600
INVITATION_TTL_DAYS = 7
OUTBOX_RETENTION_DAYS = env.outbox_retention_days
