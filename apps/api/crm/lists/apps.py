from django.apps import AppConfig


class CrmListsConfig(AppConfig):
    name = "crm.lists"
    label = "crm_lists"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.lists import tasks  # noqa: F401 - registers Celery tasks
