import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from crm.contacts.models import Contact
from crm.inbox.models import Message
from crm.pipelines.models import Opportunity
from workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def test_demo_data_fills_the_workspace_and_a_second_run_adds_nothing(workspace: Workspace) -> None:
    call_command("seed_crm_demo", workspace=workspace.slug)
    first = (
        Contact.objects.for_workspace(workspace.pk).count(),
        Opportunity.objects.for_workspace(workspace.pk).count(),
        Message.objects.for_workspace(workspace.pk).count(),
    )
    assert all(first)
    call_command("seed_crm_demo", workspace=workspace.slug)
    again = (
        Contact.objects.for_workspace(workspace.pk).count(),
        Opportunity.objects.for_workspace(workspace.pk).count(),
        Message.objects.for_workspace(workspace.pk).count(),
    )
    assert again == first


def test_demo_data_refuses_production_and_unknown_workspaces(workspace: Workspace) -> None:
    with override_settings(APP_ENVIRONMENT="prod"), pytest.raises(CommandError):
        call_command("seed_crm_demo", workspace=workspace.slug)
    with pytest.raises(CommandError):
        call_command("seed_crm_demo", workspace="no-such-workspace")
