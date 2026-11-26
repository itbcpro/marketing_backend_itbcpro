"""Reusable migration operations."""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import ProjectState

from core.tenancy import POLICY_NAME, SESSION_VARIABLE

ENABLE_SQL = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY {policy} ON {table}
    USING ({column} = NULLIF(current_setting('{variable}', true), '')::uuid)
    WITH CHECK ({column} = NULLIF(current_setting('{variable}', true), '')::uuid);
"""
DISABLE_SQL = """
DROP POLICY IF EXISTS {policy} ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


class EnableRowLevelSecurity(migrations.operations.base.Operation):
    """Isolate a tenant table: rows are visible only for the workspace set in the session."""

    reversible = True

    def __init__(self, model_name: str, column: str = "workspace_id") -> None:
        self.model_name = model_name
        self.column = column

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        return None

    def _table(self, app_label: str, state: ProjectState, editor: BaseDatabaseSchemaEditor) -> str:
        model = state.apps.get_model(app_label, self.model_name)
        return editor.quote_name(model._meta.db_table)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        table = self._table(app_label, to_state, schema_editor)
        schema_editor.execute(
            ENABLE_SQL.format(
                table=table,
                policy=POLICY_NAME,
                column=schema_editor.quote_name(self.column),
                variable=SESSION_VARIABLE,
            )
        )

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        table = self._table(app_label, from_state, schema_editor)
        schema_editor.execute(DISABLE_SQL.format(table=table, policy=POLICY_NAME))

    def describe(self) -> str:
        return f"Enable row level security on {self.model_name}"
