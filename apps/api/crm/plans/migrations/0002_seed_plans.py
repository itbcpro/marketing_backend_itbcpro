"""The starting plans. They are rows, so an operator changes prices and limits without a deploy."""

from django.db import migrations

MICROS = 1_000_000
PLANS = [
    # key, name, monthly price, limits (a missing limit means unlimited)
    ("free", "Free", 0, {"messages_per_month": 300, "contacts": 1000, "workflows": 2}),
    ("starter", "Starter", 49 * MICROS, {"messages_per_month": 3000, "contacts": 5000, "workflows": 10}),
    ("growth", "Growth", 149 * MICROS, {"messages_per_month": 30000, "contacts": 50000, "workflows": 50}),
    ("scale", "Scale", 399 * MICROS, {}),
]


def seed(apps, schema_editor):
    Plan = apps.get_model("crm_plans", "Plan")
    for position, (key, name, price, limits) in enumerate(PLANS):
        Plan.objects.get_or_create(
            key=key,
            defaults={"name": name, "price_micros": price, "limits": limits, "position": position},
        )


def unseed(apps, schema_editor):
    apps.get_model("crm_plans", "Plan").objects.filter(key__in=[p[0] for p in PLANS]).delete()


class Migration(migrations.Migration):
    dependencies = [("crm_plans", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
