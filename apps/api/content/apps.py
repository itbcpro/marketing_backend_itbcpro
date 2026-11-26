from django.apps import AppConfig


class ContentConfig(AppConfig):
    name = "content"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from content import actions, body_action
        from content.blog import openai_images, publish
        from content.google import pagespeed
        from content.google import provider as google_provider
        from content.wordpress import provider

        provider.register()
        google_provider.register()
        pagespeed.register()
        openai_images.register()
        actions.register_action_kinds()
        body_action.register_action_kinds()
        publish.register_action_kinds()
