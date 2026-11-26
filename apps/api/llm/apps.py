from django.apps import AppConfig


class LlmConfig(AppConfig):
    name = "llm"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from llm import provider

        provider.register()
