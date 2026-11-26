"""Email digests of unread notifications."""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext as _

from core.tasks import reliable_task
from crm.notifications import services
from crm.notifications.models import NotificationKind
from notifications import email as mailer
from workspaces.models import Membership, Workspace

QUEUE = "default"


def kind_label(kind: str) -> str:
    return {
        NotificationKind.TASK_ASSIGNED: _("A task was assigned to you"),
        NotificationKind.MENTION: _("You were mentioned in a note"),
        NotificationKind.CONTACT_ASSIGNED: _("A contact was assigned to you"),
        NotificationKind.DEAL_ASSIGNED: _("A deal was assigned to you"),
        NotificationKind.CONVERSATION_ASSIGNED: _("A conversation was assigned to you"),
        NotificationKind.CUSTOMER_REPLIED: _("A customer replied"),
    }[NotificationKind(kind)]


def render_items(counts: dict[str, int]) -> str:
    return "\n".join(
        _("{label}: {count}").format(label=kind_label(kind), count=count)
        for kind, count in sorted(counts.items())
    )


@reliable_task("crm.notifications.send_digests", queue=QUEUE)
def send_digests(self: Any) -> int:
    """One email per person and workspace with what is still unread. Returns emails sent."""
    sent = 0
    for digest in services.due_digests():
        member = (
            Membership.objects.select_related("user")
            .filter(workspace_id=digest.workspace_id, user_id=digest.user_id)
            .first()
        )
        workspace = Workspace.objects.filter(pk=digest.workspace_id).first()
        if member is None or workspace is None or not member.user.email:
            services.mark_emailed(digest.workspace_id, digest.notification_ids)
            continue
        mailer.send_templated_email(
            member.user.email,
            "crm_digest",
            {
                "workspace": workspace.name,
                "count": sum(digest.counts.values()),
                "items": render_items(digest.counts),
            },
        )
        services.mark_emailed(digest.workspace_id, digest.notification_ids)
        sent += 1
    return sent
