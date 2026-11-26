from django.apps import AppConfig


class AdsConfig(AppConfig):
    name = "ads"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from ads.google import actions as google_actions
        from ads.google import provider as google_provider
        from ads.meta import actions as meta_actions
        from ads.meta import provider as meta_provider
        from ads.openai import actions, provider, tools

        actions.register_action_kinds()
        tools.register_tools()
        provider.register()
        google_provider.register()
        meta_provider.register()
        meta_actions.register_action_kinds()
        google_actions.register_action_kinds()
