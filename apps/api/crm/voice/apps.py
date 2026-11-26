from django.apps import AppConfig


class CrmVoiceConfig(AppConfig):
    name = "crm.voice"
    label = "crm_voice"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.voice import events  # noqa: F401 - registers event types
