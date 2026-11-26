from django.apps import AppConfig


class CrmFieldsConfig(AppConfig):
    name = "crm.fields"
    label = "crm_fields"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.fields import events  # noqa: F401 - registers event types
