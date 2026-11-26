from django.apps import AppConfig


class MemoryConfig(AppConfig):
    name = "memory"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from audit import events
        from memory import services

        events.subscribe(events.EventTopic.ACTION_DECIDED, services.record_feedback)
