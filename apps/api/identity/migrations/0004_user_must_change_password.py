from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("identity", "0003_refresh_tokens")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="must_change_password",
            field=models.BooleanField(
                default=False, help_text="Set when an administrator chose the password."
            ),
        ),
    ]
