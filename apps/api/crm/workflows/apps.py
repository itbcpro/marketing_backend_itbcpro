from django.apps import AppConfig


class CrmWorkflowsConfig(AppConfig):
    name = "crm.workflows"
    label = "crm_workflows"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from crm.workflows import agent_actions, consumers

        consumers.register()
        agent_actions.register_action_kinds()
