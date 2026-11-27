"""Validated environment configuration.

All runtime configuration enters the process through this module. Invalid or missing
configuration stops the process at startup with a readable list of problems.
"""

import base64
import binascii
from functools import lru_cache
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "prod"]
JwtAlgorithm = Literal["EdDSA", "ES256", "RS256"]

DEFAULT_PROVIDER_API_VERSIONS: dict[str, str] = {
    "meta": "v26.0",
    "instagram": "v26.0",
    "google_ads": "v25",
    "openai_ads": "v1",
    "linkedin": "202604",
}

DEFAULT_MODEL_ROUTING: dict[str, str] = {
    "planner": "claude-sonnet-5-5",
    "writer": "claude-sonnet-5-5",
    "analyst": "claude-sonnet-5-5",
    "fast_classifier": "claude-haiku-4-5-20251001",
}

MIN_SECRET_KEY_LENGTH = 50
VAULT_KEY_BYTES = 32
_INSECURE_SECRET = "insecure-development-secret-key-change-me-0000000000000000000000"


class ConfigurationError(RuntimeError):
    """Raised when the environment does not describe a valid configuration."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    environment: Environment = "dev"
    debug: bool = False
    secret_key: SecretStr = SecretStr(_INSECURE_SECRET)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    csrf_trusted_origins: list[str] = Field(default_factory=list)
    admin_enabled: bool = False

    database_url: str = "postgres://app:app@localhost:54329/app"
    redis_url: str = "redis://localhost:16379/0"

    vault_master_keys: SecretStr = SecretStr("")
    vault_active_key_version: str = "v1"

    default_locale: str = "en"
    supported_locales: list[str] = Field(default_factory=lambda: ["en"])
    time_zone: str = "UTC"

    sentry_dsn: SecretStr = SecretStr("")
    log_level: str = "INFO"
    metrics_token: SecretStr = SecretStr("")
    otel_endpoint: str = ""
    otel_service_name: str = "itbcpro-api"
    max_body_bytes: int = 1_000_000
    media_root: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_ads_developer_token: SecretStr = SecretStr("")
    google_pagespeed_api_key: SecretStr = SecretStr("")
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    instagram_app_id: str = ""
    instagram_app_secret: SecretStr = SecretStr("")
    instagram_webhook_verify_token: SecretStr = SecretStr("")
    public_base_url: str = ""
    image_model: str = "gpt-image-2"
    image_quality: str = "medium"
    media_max_upload_bytes: int = 600_000_000
    media_max_image_bytes: int = 25_000_000
    anthropic_api_key: SecretStr = SecretStr("")
    llm_daily_budget_micros: int = 0
    effort_by_tier: dict[str, str] = Field(
        default_factory=lambda: {
            "planner": "medium",
            "writer": "medium",
            "analyst": "medium",
            "fast_classifier": "low",
        }
    )
    audit_retention_days: int = 0
    provider_sunset_dates: dict[str, str] = Field(default_factory=dict)
    provider_doc_urls: dict[str, str] = Field(
        default_factory=lambda: {
            "openai_ads_overview": "https://developers.openai.com/ads/api-overview",
            "openai_ads_campaigns": "https://developers.openai.com/ads/api-reference/campaigns",
            "google_ads_release_notes": "https://developers.google.com/google-ads/api/docs/release-notes",
            "meta_marketing_changelog": "https://developers.facebook.com/documentation/ads-commerce/marketing-api/marketing-api-changelog",
        }
    )
    mfa_required_roles: list[str] = Field(default_factory=lambda: ["owner"])
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    password_breach_check: bool = False
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Celurio Marketing"
    db_runtime_role: str = Field(default="app_rt", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    crm_default_phone_region: str = Field(default="CA", pattern=r"^[A-Z]{2}$")
    crm_template_fallback: str = ""
    crm_wallet_currency: str = Field(default="CAD", pattern=r"^[A-Z]{3}$")
    crm_campaign_batch_size: int = Field(default=50, ge=1, le=1000)
    crm_campaign_batch_interval: int = Field(default=60, ge=10)
    crm_campaign_defer_seconds: int = Field(default=300, ge=10)
    crm_email_dns_profile: dict[str, Any] = Field(default_factory=dict)
    crm_inbox_recent_days: int = Field(default=7, ge=1, le=90)
    crm_contacts_bulk_limit: int = Field(default=200, ge=1, le=1000)
    crm_inbox_bulk_limit: int = Field(default=100, ge=1, le=500)
    crm_dns_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # Connected mailboxes (crm.mailboxes). OAuth apps are registered by the operator; without a
    # client id the provider is simply not offered.
    crm_google_client_id: str = ""
    crm_google_client_secret: SecretStr = SecretStr("")
    crm_microsoft_client_id: str = ""
    crm_microsoft_client_secret: SecretStr = SecretStr("")
    crm_microsoft_tenant: str = "common"
    crm_mailbox_redirect_uri: str = ""
    crm_mailbox_return_url: str = ""
    crm_mailbox_allow_private_hosts: bool = False
    crm_mailbox_default_limits: dict[str, int] = Field(
        default_factory=lambda: {"smtp": 500, "google": 400, "microsoft": 500}
    )
    crm_mailbox_oauth_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    crm_mailbox_sync_days: int = Field(default=2, ge=1, le=30)
    crm_mailbox_sync_batch: int = Field(default=50, ge=1, le=200)
    crm_unsubscribe_max_age_days: int = Field(default=730, ge=1)
    crm_webhook_tolerance_seconds: int = Field(default=300, ge=30, le=3600)
    crm_webhook_max_body_bytes: int = Field(default=10_000_000, ge=1024)
    crm_send_per_minute: int = Field(default=60, ge=1)
    crm_deliver_max_retries: int = Field(default=5, ge=0, le=20)
    crm_filter_max_conditions: int = Field(default=50, ge=1, le=500)
    crm_export_max_rows: int = Field(default=50_000, ge=1)
    crm_bulk_max_records: int = Field(default=50_000, ge=1)
    crm_bulk_chunk_size: int = Field(default=200, ge=1, le=5000)
    crm_import_max_bytes: int = Field(default=5_000_000, ge=1)
    crm_import_max_rows: int = Field(default=50_000, ge=1)
    crm_import_error_limit: int = Field(default=1000, ge=1)
    crm_default_law_profile: str = Field(default="ca", pattern=r"^[a-z]{2,10}$")
    crm_merge_undo_hours: int = Field(default=72, ge=0, le=720)
    crm_stale_deal_days: int = Field(default=14, ge=1, le=365)
    crm_insights_days: int = Field(default=90, ge=1, le=730)
    crm_digest_min_age_minutes: int = Field(default=60, ge=1, le=1440)
    crm_twilio_api_base: str = Field(default="https://api.twilio.com")
    crm_sms_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    crm_voice_ring_seconds: int = Field(default=25, ge=5, le=120)
    crm_tracking_max_age_days: int = Field(default=730, ge=1, le=3650)
    crm_tracking_retention_days: int = Field(default=540, ge=1, le=3650)
    crm_health_outbox_lag_seconds: int = Field(default=300, ge=10, le=86400)
    crm_health_overdue_runs: int = Field(default=10, ge=0, le=100000)
    crm_health_overdue_after_seconds: int = Field(default=300, ge=30, le=86400)
    crm_health_failure_rate: float = Field(default=0.2, gt=0, le=1)
    crm_health_min_messages: int = Field(default=20, ge=1, le=100000)
    crm_webhook_retention_days: int = Field(default=30, ge=1, le=3650)
    crm_processed_event_retention_days: int = Field(default=60, ge=7, le=3650)
    crm_workflow_retention_days: int = Field(default=365, ge=1, le=3650)
    crm_notification_retention_days: int = Field(default=90, ge=1, le=3650)
    crm_recording_retention_days: int = Field(default=180, ge=1, le=3650)
    crm_report_default_days: int = Field(default=30, ge=1, le=366)
    crm_report_max_days: int = Field(default=366, ge=1, le=3660)
    crm_workflow_max_steps: int = Field(default=100, ge=1, le=500)
    crm_workflow_max_wait_days: int = Field(default=365, ge=1, le=3650)
    crm_workflow_run_step_limit: int = Field(default=50, ge=1, le=500)
    crm_workflow_max_attempts: int = Field(default=5, ge=1, le=20)
    crm_workflow_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    crm_workflow_blocked_retry_minutes: int = Field(default=60, ge=1, le=1440)
    crm_workflow_lost_after_seconds: int = Field(default=120, ge=30, le=3600)
    crm_meta_graph_base: str = Field(default="https://graph.facebook.com")
    crm_meta_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    crm_voicemail_max_seconds: int = Field(default=120, ge=10, le=600)
    crm_restore_days: int = Field(default=30, ge=1, le=365)
    crm_max_tags_per_request: int = Field(default=50, ge=1, le=500)
    jwt_issuer: str = "itbcpro-api"
    jwt_audience: str = "itbcpro"
    jwt_algorithm: JwtAlgorithm = "EdDSA"
    jwt_signing_keys: SecretStr = SecretStr("")
    jwt_active_kid: str = ""
    jwt_access_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    jwt_refresh_ttl_days: int = Field(default=30, ge=1, le=90)
    jwt_refresh_idle_days: int = Field(default=7, ge=1, le=90)
    jwt_leeway_seconds: int = Field(default=10, ge=0, le=60)
    outbox_retention_days: int = 14
    email_backend: str = "django.core.mail.backends.console.EmailBackend"
    default_from_email: str = "no-reply@itbcpro.test"
    web_base_url: str = "http://localhost:3000"
    signup_enabled: bool = False

    provider_api_versions: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_API_VERSIONS)
    )
    model_routing: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_MODEL_ROUTING))

    @model_validator(mode="after")
    def _validate_deployed_environments(self) -> Self:
        problems: list[str] = []
        if self.environment in {"staging", "prod"}:
            problems.extend(self._deployed_problems())
        problems.extend(self._vault_key_problems())
        problems.extend(self._jwt_key_problems())
        if self.default_locale not in self.supported_locales:
            problems.append("APP_DEFAULT_LOCALE must be one of APP_SUPPORTED_LOCALES")
        if urlparse(self.database_url).scheme not in {"postgres", "postgresql"}:
            problems.append("APP_DATABASE_URL must be a postgres URL")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _vault_key_problems(self) -> list[str]:
        raw = self.vault_master_keys.get_secret_value()
        if not raw:
            return []
        try:
            keys = self.parsed_vault_keys()
        except ValueError as exc:
            return [str(exc)]
        if self.vault_active_key_version not in keys:
            return ["APP_VAULT_ACTIVE_KEY_VERSION is not present in APP_VAULT_MASTER_KEYS"]
        return []

    def _deployed_problems(self) -> list[str]:
        problems: list[str] = []
        if self.secret_key.get_secret_value() == _INSECURE_SECRET:
            problems.append("APP_SECRET_KEY must be set")
        elif len(self.secret_key.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            problems.append(f"APP_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters")
        if not self.vault_master_keys.get_secret_value():
            problems.append("APP_VAULT_MASTER_KEYS must be set")
        if self.debug:
            problems.append("APP_DEBUG must be false")
        if not self.csrf_trusted_origins:
            problems.append("APP_CSRF_TRUSTED_ORIGINS must be set")
        if not self.jwt_signing_keys.get_secret_value():
            problems.append("APP_JWT_SIGNING_KEYS must be set")
        return problems

    def parsed_jwt_keys(self) -> dict[str, bytes]:
        """Parse ``kid:base64(PEM private key)`` pairs separated by commas."""
        keys: dict[str, bytes] = {}
        raw = self.jwt_signing_keys.get_secret_value()
        for pair in filter(None, (part.strip() for part in raw.split(","))):
            kid, sep, encoded = pair.partition(":")
            if not sep or not kid:
                raise ValueError("APP_JWT_SIGNING_KEYS entries must look like kid:base64pem")
            try:
                pem = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"jwt key {kid} is not valid base64") from exc
            if b"PRIVATE KEY" not in pem:
                raise ValueError(f"jwt key {kid} must be a PEM private key")
            keys[kid] = pem
        return keys

    def _jwt_key_problems(self) -> list[str]:
        if not self.jwt_signing_keys.get_secret_value():
            return []
        try:
            keys = self.parsed_jwt_keys()
        except ValueError as exc:
            return [str(exc)]
        if self.jwt_active_kid not in keys:
            return ["APP_JWT_ACTIVE_KID is not present in APP_JWT_SIGNING_KEYS"]
        return []

    def parsed_vault_keys(self) -> dict[str, bytes]:
        """Parse ``version:base64key`` pairs separated by commas into raw 32-byte keys."""
        keys: dict[str, bytes] = {}
        raw = self.vault_master_keys.get_secret_value()
        for pair in filter(None, (part.strip() for part in raw.split(","))):
            version, sep, encoded = pair.partition(":")
            if not sep or not version:
                raise ValueError("APP_VAULT_MASTER_KEYS entries must look like version:base64key")
            try:
                key = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"vault key {version} is not valid base64") from exc
            if len(key) != VAULT_KEY_BYTES:
                raise ValueError(
                    f"vault key {version} must decode to exactly {VAULT_KEY_BYTES} bytes"
                )
            keys[version] = key
        return keys


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        lines = [
            f"- {'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigurationError("Invalid configuration:\n" + "\n".join(lines)) from exc
