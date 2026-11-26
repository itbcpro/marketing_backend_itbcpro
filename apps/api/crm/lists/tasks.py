"""Background jobs of the lists module. Each runs inside the tenant of its workspace."""

from typing import Any

from core.tasks import tenant_task


@tenant_task("crm.lists.run_import", queue="default")
def run_import_task(self: Any, *, workspace_id: str, job_id: str) -> None:
    import uuid

    from crm.lists import imports

    imports.run(workspace_id=uuid.UUID(workspace_id), job_id=uuid.UUID(job_id))


@tenant_task("crm.lists.run_bulk", queue="default")
def run_bulk_task(self: Any, *, workspace_id: str, job_id: str) -> None:
    import uuid

    from crm.lists import bulk

    bulk.run(workspace_id=uuid.UUID(workspace_id), job_id=uuid.UUID(job_id))
