from django.apps import AppConfig


class SocialConfig(AppConfig):
    name = "social"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from social import actions, images, tools
        from social.instagram import provider

        provider.register()
        images.register()
        actions.register_action_kinds()
        tools.register_tools()
