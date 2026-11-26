from django.apps import AppConfig


class AgentsConfig(AppConfig):
    name = "agents"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from agents import standard_tools  # noqa: F401 - registers the standard tools
