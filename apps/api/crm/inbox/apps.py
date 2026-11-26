from django.apps import AppConfig


class CrmInboxConfig(AppConfig):
    name = "crm.inbox"
    label = "crm_inbox"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.inbox import consumers, events  # noqa: F401 - registers events and handlers

        consumers.register()
