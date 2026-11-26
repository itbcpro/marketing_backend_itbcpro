import core.ids
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from core.migration_ops import EnableRowLevelSecurity


class Migration(migrations.Migration):

    dependencies = [
        ('crm_activity', '0003_note_opportunity'),
        ('crm_contacts', '0001_initial'),
        ('crm_pipelines', '0002_deal_and_stage_fields'),
        ('workspaces', '0006_organization_required'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Appointment',
            fields=[
                ('id', models.UUIDField(default=core.ids.new_id, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(max_length=200)),
                ('starts_at', models.DateTimeField()),
                ('ends_at', models.DateTimeField()),
                ('location', models.CharField(blank=True, default='', max_length=300)),
                ('notes', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('scheduled', 'Scheduled'), ('completed', 'Completed'), ('cancelled', 'Cancelled'), ('no_show', 'No Show')], default='scheduled', max_length=12)),
                ('assigned_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('contact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='appointments', to='crm_contacts.contact')),
                ('opportunity', models.ForeignKey(blank=True, help_text='The deal this appointment is about, when it is about one.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='crm_pipelines.opportunity')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='workspaces.workspace')),
            ],
            options={
                'ordering': ['starts_at'],
                'indexes': [models.Index(fields=['workspace', 'starts_at'], name='appt_workspace_start_idx'), models.Index(fields=['contact', 'starts_at'], name='appt_contact_idx'), models.Index(fields=['opportunity', 'starts_at'], name='appt_deal_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('status__in', ['scheduled', 'completed', 'cancelled', 'no_show'])), name='appointment_status_valid'), models.CheckConstraint(condition=models.Q(('ends_at__gt', models.F('starts_at'))), name='appointment_ends_after_start')],
            },
        ),
        EnableRowLevelSecurity("Appointment"),
    ]
