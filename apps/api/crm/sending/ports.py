"""The channel adapter port: what the gateway needs from a provider, nothing more."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


class AdapterError(Exception):
    """A provider refused or failed. ``code`` is a stable machine code stored on the message."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class RetryableAdapterError(AdapterError):
    """Temporary trouble (timeout, provider outage, throttling): the delivery job tries again."""

    def __init__(self, code: str) -> None:
        super().__init__(code, retryable=True)


class PermanentAdapterError(AdapterError):
    """The provider will never accept this message (bad number, rejected content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code, retryable=False)


@dataclass(frozen=True)
class Capabilities:
    """What a channel cannot do, declared up front instead of failing at send time."""

    max_body_length: int
    supports_subject: bool = False
    supports_attachments: bool = False


@dataclass(frozen=True)
class OutboundRequest:
    workspace_id: uuid.UUID
    message_id: uuid.UUID
    channel: str
    to: str
    body: str
    subject: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    from_address: str = ""
    # The provider id of the last message the contact sent us, for reply threading.
    in_reply_to: str = ""
    # One-click unsubscribe link for channels that carry it (email).
    unsubscribe_url: str = ""
    # An HTML version of the body, only when the message is tracked (email).
    html: str = ""


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str


class ChannelAdapter(Protocol):
    channel: str
    capabilities: Capabilities

    def send(self, request: OutboundRequest) -> SendResult:
        """Hand the message to the provider. Raise ``AdapterError`` subclasses on failure.

        Implementations must be safe to call again for the same ``message_id``: the delivery job
        retries after a crash, and the provider must not send twice.
        """
        ...
