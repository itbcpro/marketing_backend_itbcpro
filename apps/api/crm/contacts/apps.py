from django.apps import AppConfig


class CrmContactsConfig(AppConfig):
    name = "crm.contacts"
    label = "crm_contacts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.contacts import events  # noqa: F401 - registers event types
