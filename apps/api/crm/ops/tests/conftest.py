import pytest
from django.db import transaction

from crm.workflows import engine


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):
    """Tests drive the engine themselves: queueing is recorded, not sent to a broker."""
    sent: list[tuple[object, object]] = []
    monkeypatch.setattr(engine, "_dispatch", lambda ws, eid: sent.append((ws, eid)))
    monkeypatch.setattr(transaction, "on_commit", lambda func, *args, **kwargs: func())
    return sent
