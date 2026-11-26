import core.ids
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from core.migration_ops import EnableRowLevelSecurity


class Migration(migrations.Migration):

    dependencies = [
        ('crm_contacts', '0001_initial'),
        ('crm_pipelines', '0002_deal_and_stage_fields'),
        ('workspaces', '0006_organization_required'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Payment',
            fields=[
                ('id', models.UUIDField(default=core.ids.new_id, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(choices=[('payment', 'Payment'), ('refund', 'Refund')], max_length=10)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('paid', 'Paid'), ('failed', 'Failed')], default='paid', max_length=10)),
                ('method', models.CharField(choices=[('cash', 'Cash'), ('bank_transfer', 'Bank Transfer'), ('card', 'Card'), ('cheque', 'Cheque'), ('other', 'Other')], default='other', max_length=20)),
                ('amount_micros', models.BigIntegerField()),
                ('currency', models.CharField(max_length=3)),
                ('reference', models.CharField(blank=True, default='', max_length=100)),
                ('note', models.TextField(blank=True, default='')),
                ('occurred_at', models.DateTimeField()),
                ('contact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payments', to='crm_contacts.contact')),
                ('opportunity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payments', to='crm_pipelines.opportunity')),
                ('recorded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='workspaces.workspace')),
            ],
            options={
                'indexes': [models.Index(fields=['opportunity', '-occurred_at'], name='payment_deal_idx'), models.Index(fields=['contact', '-occurred_at'], name='payment_contact_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('kind__in', ['payment', 'refund'])), name='payment_kind_valid'), models.CheckConstraint(condition=models.Q(('status__in', ['pending', 'paid', 'failed'])), name='payment_status_valid'), models.CheckConstraint(condition=models.Q(('method__in', ['cash', 'bank_transfer', 'card', 'cheque', 'other'])), name='payment_method_valid'), models.CheckConstraint(condition=models.Q(('amount_micros__gt', 0)), name='payment_amount_positive')],
            },
        ),
        EnableRowLevelSecurity("Payment"),
    ]
