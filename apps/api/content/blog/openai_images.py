"""Image generation through the OpenAI Images API, with the key kept as a workspace connection."""

import base64
import binascii
import json
import uuid
from typing import Any

import httpx

from connections.models import Connection, ConnectionStatus, CredentialKind
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    ProviderDescriptor,
    register_provider,
)
from connections.services import read_secret
from content import net
from core.errors import (
    AuthExpiredError,
    DomainError,
    PermanentProviderError,
    RetryableProviderError,
    ValidationFailedError,
)
from core.security import UnsafeUrlError

PROVIDER = "openai_images"
BASE = "https://api.openai.com/v1"
MODEL = "gpt-image-1"
SIZES = {"landscape": "1536x1024", "square": "1024x1024", "portrait": "1024x1536"}
QUALITY = "medium"
TIMEOUT = 240.0
MAX_BYTES = 30_000_000
HTTP_UNAUTHORISED = 401
HTTP_BAD_REQUEST = 400
HTTP_TOO_MANY = 429
HTTP_SERVER_ERROR = 500
HTTP_ERROR = 400
NO_CREDITS = "openai_no_credits"
CREDIT_CODES = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}


def _connection(workspace_id: uuid.UUID) -> Connection | None:
    return (
        Connection.objects.filter(workspace_id=workspace_id, provider=PROVIDER)
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )


def has_key(workspace_id: uuid.UUID) -> bool:
    return _connection(workspace_id) is not None


def _key(workspace_id: uuid.UUID) -> str:
    connection = _connection(workspace_id)
    if connection is None:
        raise ValidationFailedError("connect an OpenAI API key to generate images")
    return read_secret(connection, CredentialKind.API_KEY)


def _call(method: str, path: str, key: str, body: Any = None) -> dict[str, Any]:
    try:
        response = net.fetch(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=TIMEOUT,
            max_bytes=MAX_BYTES,
            method=method,
            json_body=body,
        )
    except UnsafeUrlError as exc:
        raise PermanentProviderError(str(exc), provider=PROVIDER) from exc
    except httpx.TimeoutException as exc:
        raise RetryableProviderError("openai did not answer in time", provider=PROVIDER) from exc
    except httpx.HTTPError as exc:
        raise RetryableProviderError("openai could not be reached", provider=PROVIDER) from exc
    if response.status_code >= HTTP_ERROR:
        raise _error(response)
    try:
        data = json.loads(response.content)
    except ValueError as exc:
        raise PermanentProviderError("openai returned invalid JSON", provider=PROVIDER) from exc
    return data if isinstance(data, dict) else {}


def _error(response: httpx.Response) -> Exception:
    code = ""
    try:
        body = json.loads(response.content)
        error = body.get("error") if isinstance(body, dict) else None
        inner = error or {}
        code = str(inner.get("code") or inner.get("type") or "")
        if str(inner.get("type", "")) in CREDIT_CODES:
            code = str(inner["type"])
    except ValueError:
        pass
    status = response.status_code
    if (status == HTTP_TOO_MANY and code in CREDIT_CODES) or "credit_balance" in response.text:
        return PermanentProviderError(NO_CREDITS, provider=PROVIDER)
    if status == HTTP_UNAUTHORISED:
        return AuthExpiredError("the OpenAI API key was rejected", provider=PROVIDER)
    if status == HTTP_BAD_REQUEST:
        return PermanentProviderError(code or "image_refused", provider=PROVIDER)
    if status == HTTP_TOO_MANY or status >= HTTP_SERVER_ERROR:
        return RetryableProviderError(f"openai returned {status}", provider=PROVIDER)
    return PermanentProviderError(code or f"openai returned {status}", provider=PROVIDER)


def generate(workspace_id: uuid.UUID, prompt: str, *, shape: str = "landscape") -> bytes:
    """One image as WebP bytes."""
    data = _call(
        "POST",
        "/images/generations",
        _key(workspace_id),
        {
            "model": MODEL,
            "prompt": prompt,
            "size": SIZES.get(shape, SIZES["landscape"]),
            "quality": QUALITY,
            "output_format": "webp",
            "n": 1,
        },
    )
    rows = data.get("data")
    first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    try:
        return base64.b64decode(str(first.get("b64_json", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PermanentProviderError("openai returned no image", provider=PROVIDER) from exc


def identify(api_key: str) -> tuple[str, str]:
    try:
        _call("GET", "/models", api_key)
    except AuthExpiredError as exc:
        raise ValidationFailedError("OpenAI rejected this API key") from exc
    except DomainError as exc:
        raise ValidationFailedError(str(exc)[:200]) from exc
    return "openai-images", "OpenAI API key (images)"


def health_check(connection: Connection) -> HealthResult:
    try:
        _call("GET", "/models", read_secret(connection, CredentialKind.API_KEY))
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "the API key was rejected")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="OpenAI API key (blog images)",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="api_key",
                    label="OpenAI API key",
                    secret=True,
                    help_text=(
                        "From platform.openai.com, API keys. It is used only to draw the images "
                        "of blog articles. The organisation needs access to the gpt-image-1 model."
                    ),
                ),
            ),
            identify=identify,
            health_check=health_check,
        )
    )
