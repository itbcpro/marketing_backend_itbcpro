from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from core import tasks  # noqa: F401 - registers Celery signal handlers
        from core.logging import configure_logging
        from core.observability import configure_tracing, init_sentry

        configure_logging()
        init_sentry()
        configure_tracing()
