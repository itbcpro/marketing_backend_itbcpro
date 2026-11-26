"""Notification port and adapters."""

from dataclasses import dataclass, field
from typing import Protocol

from notifications import email


class Notifier(Protocol):
    """Deliver a catalog-based message to a recipient. Implementations must not raise for unknown
    recipients; they raise ``KeyError`` only for an unknown template key."""

    def send(self, recipient: str, template_key: str, context: dict[str, object]) -> None: ...


class EmailNotifier:
    def send(self, recipient: str, template_key: str, context: dict[str, object]) -> None:
        email.send_templated_email(recipient, template_key, context)


@dataclass
class RecordingNotifier:
    """Fake adapter that records what would have been sent."""

    sent: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def send(self, recipient: str, template_key: str, context: dict[str, object]) -> None:
        email.render(template_key, context)  # fail loudly on unknown keys or missing placeholders
        self.sent.append((recipient, template_key, context))
