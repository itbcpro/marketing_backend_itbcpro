"""Contract tests every channel adapter must pass (ChannelAdapter port).

Subclass ``AdapterContract`` in the adapter's test module and implement ``make_adapter``.
"""

from __future__ import annotations

import uuid

from crm.sending.ports import ChannelAdapter, OutboundRequest, SendResult


class AdapterContract:
    channel = "email"
    recipient = "person@example.com"

    def make_adapter(self) -> ChannelAdapter:
        raise NotImplementedError

    def request(self, **overrides: object) -> OutboundRequest:
        values: dict[str, object] = {
            "workspace_id": uuid.uuid4(),
            "message_id": uuid.uuid4(),
            "channel": self.channel,
            "to": self.recipient,
            "body": "Hello from the contract test",
            "subject": "Contract",
        }
        values.update(overrides)
        return OutboundRequest(**values)  # type: ignore[arg-type]

    def test_declares_what_it_supports(self) -> None:
        adapter = self.make_adapter()
        assert adapter.channel == self.channel
        assert adapter.capabilities.max_body_length > 0

    def test_a_valid_message_returns_a_provider_id(self) -> None:
        result = self.make_adapter().send(self.request())
        assert isinstance(result, SendResult) and result.provider_message_id

    def test_different_messages_get_different_provider_ids(self) -> None:
        adapter = self.make_adapter()
        first, second = adapter.send(self.request()), adapter.send(self.request())
        assert first.provider_message_id != second.provider_message_id

    def test_replies_can_reference_the_message_they_answer(self) -> None:
        result = self.make_adapter().send(self.request(in_reply_to="<earlier@example.com>"))
        assert result.provider_message_id
