from django.apps import AppConfig


class CrmPipelinesConfig(AppConfig):
    name = "crm.pipelines"
    label = "crm_pipelines"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.pipelines import events  # noqa: F401 - registers event types
