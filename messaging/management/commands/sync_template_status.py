"""Pull plantilla approval verdicts from the provider.

    python manage.py sync_template_status

The same call the Plantillas page's "Sincronizar con WhatsApp" button makes
(messaging.services.sync_template_verdicts), exposed for a cron/scheduled
job: Meta reviews templates asynchronously and reports the verdict on its own
timetable, so a nightly sync keeps the Estado column honest without anyone
opening the page.
"""

from django.core.management.base import BaseCommand, CommandError

from messaging import services
from messaging.providers.registry import get_provider


class Command(BaseCommand):
    help = "Sync plantilla approval statuses from the messaging provider."

    def handle(self, *args, **options):
        provider = get_provider()
        try:
            changed = services.sync_template_verdicts()
        except Exception as exc:  # the provider's own error is the message
            raise CommandError(f"Sync failed against {provider.name}: {exc}") from exc
        self.stdout.write(
            f"Synced against {provider.name}: {changed} plantilla(s) changed."
        )
