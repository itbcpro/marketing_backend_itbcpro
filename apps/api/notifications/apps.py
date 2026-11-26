from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "notifications"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from audit import events
        from notifications import handlers

        events.subscribe(events.EventTopic.ACTION_PROPOSED, handlers.on_action_proposed)
