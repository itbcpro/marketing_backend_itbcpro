"""A small Twilio REST client for messages. Tests and the simulator inject an httpx transport."""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

import httpx
from django.conf import settings

from crm.sms import errors

IDEMPOTENCY_HEADER = "I-Twilio-Idempotency-Token"


class SmsSendError(Exception):
    def __init__(self, mapped: errors.Mapped) -> None:
        super().__init__(mapped.code)
        self.code = mapped.code
        self.retryable = mapped.retryable


@dataclass(frozen=True)
class Sent:
    provider_message_id: str


class TwilioClient:
    def __init__(
        self, account_sid: str, auth_token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._sid = account_sid
        self._client = httpx.Client(
            base_url=settings.CRM_TWILIO_API_BASE,
            auth=(account_sid, auth_token),
            timeout=settings.CRM_SMS_TIMEOUT_SECONDS,
            transport=transport,
        )

    def send(
        self,
        *,
        sender: str,
        to: str,
        body: str,
        media_urls: list[str],
        idempotency_key: str,
        status_callback: str = "",
    ) -> Sent:
        data: dict[str, str | list[str]] = {"From": sender, "To": to, "Body": body}
        if media_urls:
            data["MediaUrl"] = media_urls
        if status_callback:
            data["StatusCallback"] = status_callback
        return self._post("Messages", data, idempotency_key)

    def call(
        self,
        *,
        sender: str,
        to: str,
        twiml_url: str,
        idempotency_key: str,
        status_callback: str = "",
    ) -> Sent:
        """Ring ``to`` and, when it answers, ask ``twiml_url`` what to do."""
        data: dict[str, str | list[str]] = {"From": sender, "To": to, "Url": twiml_url}
        if status_callback:
            data["StatusCallback"] = status_callback
        return self._post("Calls", data, idempotency_key)

    def _post(self, resource: str, data: dict[str, str | list[str]], key: str) -> Sent:
        try:
            response = self._client.post(
                f"/2010-04-01/Accounts/{self._sid}/{resource}.json",
                data=data,
                headers={IDEMPOTENCY_HEADER: key},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SmsSendError(errors.Mapped(errors.UNAVAILABLE, True)) from exc
        payload = _json(response)
        if response.is_success and payload.get("sid"):
            return Sent(str(payload["sid"]))
        code = payload.get("code")
        raise SmsSendError(
            errors.from_twilio(
                int(code) if isinstance(code, int | str) and str(code).isdigit() else None,
                response.status_code,
            )
        )


def _json(response: httpx.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def expected_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio signs the full URL followed by each POST parameter (sorted by name) as name+value."""
    text = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), text.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()
