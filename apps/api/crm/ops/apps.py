from django.apps import AppConfig


class CrmOpsConfig(AppConfig):
    name = "crm.ops"
    label = "crm_ops"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.ops import collector

        collector.register_collector()
