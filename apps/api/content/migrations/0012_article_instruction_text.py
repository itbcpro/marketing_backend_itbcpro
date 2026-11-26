from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content', '0011_ignored_check'),
    ]

    operations = [
        migrations.AlterField(
            model_name='article',
            name='instruction',
            field=models.TextField(blank=True, default=''),
        ),
    ]
