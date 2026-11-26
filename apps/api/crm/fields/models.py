"""Custom field definitions, their values, and workspace-level constants."""

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class ObjectType(models.TextChoices):
    CONTACT = "contact"
    COMPANY = "company"
    OPPORTUNITY = "opportunity"


class FieldType(models.TextChoices):
    TEXT = "text"
    LONG_TEXT = "long_text"
    NUMBER = "number"
    MONEY = "money"
    PHONE = "phone"
    EMAIL = "email"
    URL = "url"
    DATE = "date"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    CHECKBOX = "checkbox"


KEY_PATTERN = r"^[a-z][a-z0-9_]{1,49}$"


class FieldDefinition(TenantModel):
    object_type = models.CharField(max_length=20, choices=ObjectType.choices)
    key = models.CharField(max_length=50)
    label = models.CharField(max_length=100)
    field_type = models.CharField(max_length=20, choices=FieldType.choices)
    options = models.JSONField(default=list)
    folder = models.CharField(max_length=100, blank=True, default="")
    position = models.PositiveIntegerField(default=0)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["FieldDefinition"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "object_type", "key"], name="uniq_field_key"
            ),
            models.CheckConstraint(
                condition=Q(object_type__in=[o.value for o in ObjectType]),
                name="field_object_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(field_type__in=[t.value for t in FieldType]),
                name="field_type_valid",
            ),
            models.CheckConstraint(condition=Q(key__regex=KEY_PATTERN), name="field_key_valid"),
        ]
        ordering = ["position", "created_at"]

    def __str__(self) -> str:
        return f"{self.object_type}.{self.key}"


class FieldValue(TenantModel):
    """One typed value of one record. Exactly the column of the field type is used."""

    object_type = models.CharField(max_length=20, choices=ObjectType.choices)
    record_id = models.UUIDField()
    field = models.ForeignKey(FieldDefinition, on_delete=models.PROTECT, related_name="values")
    value_text = models.TextField(null=True, blank=True)  # noqa: DJ001 - null means "no value"
    value_number = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    value_date = models.DateField(null=True, blank=True)
    value_json = models.JSONField(null=True, blank=True)

    objects = TenantManager["FieldValue"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["field", "record_id"], name="uniq_field_value")
        ]
        indexes = [models.Index(fields=["object_type", "record_id"], name="field_value_record_idx")]


class WorkspaceValue(TenantModel):
    """A workspace-level constant usable in templates, such as a booking link."""

    key = models.CharField(max_length=50)
    label = models.CharField(max_length=100)
    value = models.TextField(blank=True, default="")

    objects = TenantManager["WorkspaceValue"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="uniq_workspace_value_key"),
            models.CheckConstraint(
                condition=Q(key__regex=KEY_PATTERN), name="workspace_value_key_valid"
            ),
        ]
