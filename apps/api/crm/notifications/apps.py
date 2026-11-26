from django.apps import AppConfig


class CrmNotificationsConfig(AppConfig):
    name = "crm.notifications"
    label = "crm_notifications"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.notifications import consumers

        consumers.register()
