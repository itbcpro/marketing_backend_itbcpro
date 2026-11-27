from django.apps import AppConfig


class CrmSendingConfig(AppConfig):
    name = "crm.sending"
    label = "crm_sending"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.channels.email_smtp import SmtpEmailAdapter
        from crm.channels.instagram_dm import InstagramAdapter
        from crm.channels.mailbox import MailboxAdapter
        from crm.channels.meta_channels import MetaMessengerAdapter, MetaWhatsAppAdapter
        from crm.channels.twilio_sms import TwilioSmsAdapter
        from crm.channels.twilio_voice import TwilioVoiceAdapter
        from crm.consent.models import Channel
        from crm.sending import registry

        registry.register(Channel.EMAIL, SmtpEmailAdapter)
        registry.register_account_adapter("mailbox", MailboxAdapter)
        registry.register(Channel.SMS, TwilioSmsAdapter)
        registry.register(Channel.WHATSAPP, MetaWhatsAppAdapter)
        registry.register(Channel.MESSENGER, MetaMessengerAdapter)
        registry.register(Channel.INSTAGRAM, InstagramAdapter)
        registry.register(Channel.VOICE, TwilioVoiceAdapter)
