from django.apps import AppConfig


class CrmUsageConfig(AppConfig):
    name = "crm.usage"
    label = "crm_usage"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.usage import consumers, events  # noqa: F401 - registers events and handlers

        consumers.register()
