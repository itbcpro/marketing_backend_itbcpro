"""Shared pytest fixtures."""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.cache import cache
from django.test import Client

from core import tenancy
from identity.models import User
from workspaces.models import Role, Workspace
from workspaces.services import create_workspace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PASSWORD = "correct-horse-battery-staple-42"


@pytest.fixture(autouse=True)
def _isolate_process_state() -> Iterator[None]:
    """Nothing a test leaves in the process may reach the next one.

    A workspace entered without a request, and the counts the rate limits keep in the cache,
    would otherwise decide whether an unrelated test passes.
    """
    cache.clear()
    yield
    tenancy.forget()
    cache.clear()


@pytest.fixture(scope="session", autouse=True)
def compiled_catalogs() -> None:
    """Translation catalogs are compiled without GNU gettext."""
    subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "tools" / "compile_messages.py")],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def owner(db: None) -> User:
    return User.objects.create_user("owner@example.com", PASSWORD, full_name="Owner")


@pytest.fixture
def workspace(owner: User) -> Workspace:
    return create_workspace(name="Acme Marketing", owner=owner)


def make_user(email: str, workspace: Workspace | None = None, role: Role = Role.VIEWER) -> User:
    from workspaces.models import Membership

    user = User.objects.create_user(email, PASSWORD)
    if workspace is not None:
        Membership.objects.create(workspace=workspace, user=user, role=role)
    return user


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def logged_in(client: Client, owner: User) -> Client:
    client.force_login(owner)
    return client
