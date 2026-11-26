from django.apps import AppConfig


class CrmConsentConfig(AppConfig):
    name = "crm.consent"
    label = "crm_consent"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.consent import consumers, events  # noqa: F401 - registers events and handlers

        consumers.register()
