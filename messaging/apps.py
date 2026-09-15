from django.apps import AppConfig


class MessagingConfig(AppConfig):
    """The messaging layer: conversations, messages and provider integrations.

    Deliberately separate from ``core`` (the UI shell): everything that talks
    to WhatsApp/Meta lives here, so swapping or adding a provider never
    touches a view or template.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "messaging"
    verbose_name = "mensajería"

    def ready(self):
        # manage.py test sends through a stub that lives in test code
        # (messaging/testing.py). It is registered here, and only under
        # TESTING, so no other process can select it.
        from django.conf import settings

        if settings.TESTING:
            from . import testing

            testing.register()
