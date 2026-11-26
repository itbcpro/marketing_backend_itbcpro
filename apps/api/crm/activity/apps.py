from django.apps import AppConfig


class CrmActivityConfig(AppConfig):
    name = "crm.activity"
    label = "crm_activity"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.activity import consumers, events  # noqa: F401 - registers events and handlers

        consumers.register()
