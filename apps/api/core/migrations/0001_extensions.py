"""Install the PostgreSQL extensions the platform relies on."""

from django.contrib.postgres.operations import (
    BtreeGinExtension,
    CITextExtension,
    CreateExtension,
    TrigramExtension,
    UnaccentExtension,
)
from django.db import migrations


class Migration(migrations.Migration):
    dependencies: list[tuple[str, str]] = []

    operations = [
        CITextExtension(),
        TrigramExtension(),
        BtreeGinExtension(),
        UnaccentExtension(),
        CreateExtension("vector"),
    ]
