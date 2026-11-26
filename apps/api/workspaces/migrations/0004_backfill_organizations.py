"""Give every existing workspace its own organization (expand step; NOT NULL comes later)."""

from django.db import migrations

from core.ids import new_id


def forwards(apps, schema_editor):
    Organization = apps.get_model("workspaces", "Organization")
    Workspace = apps.get_model("workspaces", "Workspace")
    for workspace in Workspace.objects.filter(organization__isnull=True).iterator():
        organization = Organization.objects.create(
            id=new_id(),
            name=workspace.name,
            slug=f"org-{workspace.slug}"[:80],
            is_active=workspace.is_active,
        )
        workspace.organization = organization
        workspace.save(update_fields=["organization"])


def backwards(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    Workspace.objects.update(organization=None)


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0003_organization")]
    operations = [migrations.RunPython(forwards, backwards)]
