"""Add the pruning checkpoint table and allow a guarded prune of old audit events."""

from django.db import migrations, models

FORWARD = """
CREATE OR REPLACE FUNCTION audit_event_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('app.audit_prune', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_auditevent is append-only (% blocked)', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""

REVERSE = """
CREATE OR REPLACE FUNCTION audit_event_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditevent is append-only (% blocked)', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_audit_immutability")]

    operations = [
        migrations.CreateModel(
            name="AuditCheckpoint",
            fields=[
                ("chain", models.UUIDField(primary_key=True, serialize=False)),
                ("last_seq", models.PositiveBigIntegerField()),
                ("last_hash", models.CharField(max_length=64)),
                ("events_pruned", models.PositiveBigIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
