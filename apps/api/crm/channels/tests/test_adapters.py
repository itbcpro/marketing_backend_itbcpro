import smtplib

import pytest
from django.core import mail

from crm.channels.email_smtp import SmtpEmailAdapter
from crm.channels.fake import FakeAdapter, permanent, temporary
from crm.sending.ports import PermanentAdapterError, RetryableAdapterError
from crm.sending.tests.contract import AdapterContract


class TestFakeAdapterContract(AdapterContract):
    def make_adapter(self):
        return FakeAdapter(self.channel)


class TestEmailAdapterContract(AdapterContract):
    def make_adapter(self):
        return SmtpEmailAdapter()


class TestEmailAdapter:
    @pytest.fixture(autouse=True)
    def _sender(self, settings):
        settings.DEFAULT_FROM_EMAIL = "Acme <hello@acme.test>"

    def request(self, **extra):
        return AdapterContract().request(**extra)

    def test_sets_threading_headers_and_the_default_sender(self):
        result = SmtpEmailAdapter().send(self.request(in_reply_to="<earlier@their.test>"))
        sent = mail.outbox[-1]
        assert (
            sent.extra_headers["In-Reply-To"]
            == sent.extra_headers["References"]
            == "<earlier@their.test>"
        )
        assert sent.extra_headers["Message-ID"] == result.provider_message_id
        assert result.provider_message_id.endswith("@acme.test>")
        assert sent.from_email == "Acme <hello@acme.test>" and sent.to == ["person@example.com"]

    def test_uses_the_channel_account_address_when_given(self):
        SmtpEmailAdapter().send(self.request(from_address="sales@acme.test"))
        assert mail.outbox[-1].from_email == "sales@acme.test"

    def test_refused_recipients_are_permanent_and_outages_are_retryable(self, monkeypatch):
        def refuse(self, fail_silently=False):
            raise smtplib.SMTPRecipientsRefused({"x": (550, b"no")})

        monkeypatch.setattr("django.core.mail.EmailMessage.send", refuse)
        with pytest.raises(PermanentAdapterError):
            SmtpEmailAdapter().send(self.request())

        def down(self, fail_silently=False):
            raise ConnectionRefusedError("smtp down")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", down)
        with pytest.raises(RetryableAdapterError):
            SmtpEmailAdapter().send(self.request())


class TestFakeAdapter:
    def test_the_same_message_gets_the_same_provider_id_and_is_recorded_once(self):
        adapter = FakeAdapter("sms")
        request = AdapterContract().request(channel="sms", to="+14165550199")
        assert adapter.send(request) == adapter.send(request)
        assert len(adapter.sent) == 1

    def test_failures_are_scripted_in_order(self):
        adapter = FakeAdapter("sms")
        adapter.fail_next(temporary(), permanent("bad_number"))
        request = AdapterContract().request(channel="sms")
        with pytest.raises(RetryableAdapterError):
            adapter.send(request)
        with pytest.raises(PermanentAdapterError):
            adapter.send(request)
        assert adapter.send(request).provider_message_id
