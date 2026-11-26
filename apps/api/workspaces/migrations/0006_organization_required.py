"""Contract step: every workspace belongs to an organization (backfilled in 0004)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0005_membership_record_scope")]

    operations = [
        migrations.AlterField(
            model_name="workspace",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="workspaces",
                to="workspaces.organization",
            ),
        ),
    ]
