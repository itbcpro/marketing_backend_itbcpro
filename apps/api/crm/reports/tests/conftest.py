import pytest
from django.db import transaction


@pytest.fixture(autouse=True)
def _run_commit_hooks_at_once(monkeypatch):
    """Tests run inside one transaction, so on_commit hooks would never fire; run them now."""
    monkeypatch.setattr(transaction, "on_commit", lambda func, *args, **kwargs: func())
