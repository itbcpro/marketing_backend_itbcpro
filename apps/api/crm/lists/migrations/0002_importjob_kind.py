from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm_lists", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="importjob",
            name="kind",
            field=models.CharField(
                choices=[("contacts", "Contacts"), ("deals", "Deals")],
                default="contacts",
                max_length=10,
            ),
        ),
    ]
