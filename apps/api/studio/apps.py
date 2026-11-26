from django.apps import AppConfig


class StudioConfig(AppConfig):
    name = "studio"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from studio import bootstrap

        bootstrap.register_all()
