"""A deterministic in-memory adapter for tests and local development. It talks to no provider."""

from __future__ import annotations

import uuid

from crm.sending.ports import (
    AdapterError,
    Capabilities,
    OutboundRequest,
    PermanentAdapterError,
    RetryableAdapterError,
    SendResult,
)


class FakeAdapter:
    """Records what it was asked to send and can be told to fail the next calls."""

    def __init__(self, channel: str, *, max_body_length: int = 1000) -> None:
        self.channel = channel
        self.capabilities = Capabilities(max_body_length=max_body_length, supports_subject=True)
        self.sent: list[OutboundRequest] = []
        self.failures: list[AdapterError] = []
        self._by_message: dict[uuid.UUID, str] = {}
        self._seen: set[uuid.UUID] = set()

    def fail_next(self, *errors: AdapterError) -> None:
        self.failures.extend(errors)

    def send(self, request: OutboundRequest) -> SendResult:
        if self.failures:
            raise self.failures.pop(0)
        # The same message always gets the same provider id, like a provider honouring a retry.
        provider_id = self._by_message.setdefault(request.message_id, f"fake-{uuid.uuid4()}")
        if request.message_id not in self._seen:
            self._seen.add(request.message_id)
            self.sent.append(request)
        return SendResult(provider_message_id=provider_id)


def temporary(code: str = "provider_unavailable") -> RetryableAdapterError:
    return RetryableAdapterError(code)


def permanent(code: str = "rejected") -> PermanentAdapterError:
    return PermanentAdapterError(code)
