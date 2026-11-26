from django.apps import AppConfig


class CrmLeadsConfig(AppConfig):
    name = "crm.leads"
    label = "crm_leads"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.leads import consumers, events  # noqa: F401 - registers events and handlers

        consumers.register()
