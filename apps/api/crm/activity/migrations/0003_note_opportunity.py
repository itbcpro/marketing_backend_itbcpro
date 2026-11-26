import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm_activity', '0002_task_opportunity'),
        ('crm_pipelines', '0002_deal_and_stage_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='note',
            name='opportunity',
            field=models.ForeignKey(blank=True, help_text='The deal this note is about, when it is about one.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='crm_pipelines.opportunity'),
        ),
        migrations.AddIndex(
            model_name='note',
            index=models.Index(fields=['opportunity', '-created_at'], name='note_deal_idx'),
        ),
    ]
