import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from identity.models import User
from workspaces.models import Membership, Role, Workspace

pytestmark = pytest.mark.django_db


def test_seed_creates_one_user_per_role_and_is_idempotent() -> None:
    call_command("seed_dev")
    call_command("seed_dev")
    assert Workspace.objects.filter(slug="itbcpro").count() == 1
    roles = set(Membership.objects.values_list("role", flat=True))
    assert roles == {role.value for role in Role}
    assert User.objects.filter(email="owner@itbcpro.test").count() == 1


def test_seed_refuses_to_run_in_production() -> None:
    with override_settings(APP_ENVIRONMENT="prod"), pytest.raises(CommandError):
        call_command("seed_dev")
