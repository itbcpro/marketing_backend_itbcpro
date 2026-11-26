from django.apps import AppConfig


class CrmReportsConfig(AppConfig):
    name = "crm.reports"
    label = "crm_reports"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.reports import consumers

        consumers.register()
