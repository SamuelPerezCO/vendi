from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # Registers core.checks.retired_login_env_vars (core.W004) and
        # core.checks.public_origin_unresolved (core.W002).
        from . import checks  # noqa: F401
