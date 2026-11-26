from django.apps import AppConfig


class CrmPrivacyConfig(AppConfig):
    name = "crm.privacy"
    label = "crm_privacy"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.privacy import consumers

        consumers.register()
