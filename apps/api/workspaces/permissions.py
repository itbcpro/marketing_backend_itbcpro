"""Permission catalogue and the role-to-permission matrix (single source of authorisation)."""

from enum import StrEnum

from workspaces.models import Role


class Permission(StrEnum):
    VIEW = "view"
    EDIT_CONTENT = "edit_content"
    EDIT_MEMORY = "edit_memory"
    APPROVE_R1 = "approve_r1"
    APPROVE_R2 = "approve_r2"
    APPROVE_R3 = "approve_r3"
    MANAGE_CONNECTIONS = "manage_connections"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_GUARDRAILS = "manage_guardrails"
    CHANGE_AUTONOMY = "change_autonomy"
    USE_KILL_SWITCH = "use_kill_switch"
    VIEW_AUDIT = "view_audit"
    MANAGE_WORKSPACE = "manage_workspace"
    CRM_VIEW = "crm_view"
    CRM_EDIT = "crm_edit"
    CRM_DELETE = "crm_delete"
    CRM_IMPORT_EXPORT = "crm_import_export"
    CRM_SEND = "crm_send"
    CRM_VIEW_REPORTS = "crm_view_reports"
    CRM_MANAGE_PIPELINES = "crm_manage_pipelines"
    CRM_MANAGE_CHANNELS = "crm_manage_channels"
    CRM_MANAGE_AUTOMATION = "crm_manage_automation"
    CRM_MANAGE_SETTINGS = "crm_manage_settings"
    MANAGE_BILLING = "manage_billing"


_ALL = frozenset(Permission)

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    Role.OWNER: _ALL,
    Role.ADMIN: _ALL - {Permission.MANAGE_WORKSPACE},
    Role.APPROVER: frozenset(
        {
            Permission.VIEW,
            Permission.EDIT_CONTENT,
            Permission.APPROVE_R1,
            Permission.APPROVE_R2,
            Permission.USE_KILL_SWITCH,
            Permission.VIEW_AUDIT,
            Permission.CRM_VIEW,
            Permission.CRM_EDIT,
            Permission.CRM_SEND,
            Permission.CRM_VIEW_REPORTS,
        }
    ),
    Role.EDITOR: frozenset(
        {
            Permission.VIEW,
            Permission.EDIT_CONTENT,
            Permission.EDIT_MEMORY,
            Permission.CRM_VIEW,
            Permission.CRM_EDIT,
            Permission.CRM_SEND,
        }
    ),
    Role.ANALYST: frozenset(
        {
            Permission.VIEW,
            Permission.VIEW_AUDIT,
            Permission.CRM_VIEW,
            Permission.CRM_VIEW_REPORTS,
        }
    ),
    Role.VIEWER: frozenset({Permission.VIEW, Permission.CRM_VIEW}),
}


# Who may hand out and manage which roles. Owners exist only through platform administration, and
# nobody can raise anyone to a role they could not manage themselves (no privilege escalation).
MANAGEABLE_ROLES: dict[str, frozenset[str]] = {
    Role.OWNER: frozenset({Role.ADMIN, Role.APPROVER, Role.EDITOR, Role.ANALYST, Role.VIEWER}),
    Role.ADMIN: frozenset({Role.APPROVER, Role.EDITOR, Role.ANALYST, Role.VIEWER}),
}


def manageable_roles(role: str) -> frozenset[str]:
    return MANAGEABLE_ROLES.get(role, frozenset())


def role_allows(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
