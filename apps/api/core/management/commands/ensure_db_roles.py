"""Create or refresh the runtime database role (run after migrations, as the owner role)."""

import os

from django.core.management.base import BaseCommand
from django.db import connection

from core.db_roles import ensure_runtime_role


class Command(BaseCommand):
    help = "Create the runtime database role used with row level security and grant it access."

    def handle(self, *args: object, **options: object) -> None:
        password = os.environ.get("APP_DB_RUNTIME_PASSWORD") or None
        with connection.cursor() as cursor:
            name = ensure_runtime_role(cursor, password=password)
        self.stdout.write(f"runtime role ready: {name}")
