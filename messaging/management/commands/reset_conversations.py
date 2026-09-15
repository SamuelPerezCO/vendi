"""Empty the Inbox: delete every conversation, message and contact -- or,
with ``--demo-only``, just the demo fixtures.

Written for the moment before going live, when the Inbox holds a mix of
seeded fixtures and your own test messages and you want a clean slate that
fills up with real customers only. ``--demo-only`` is the surgical version
for a database that *already* has real customers in it (the automation that
writes into this database does not wait for a launch): it removes only what
the old ``seed_conversations`` command used to create -- contacts in the
reserved ``+5730000000xx`` range with their conversations and messages, the
"Evento de demostración." calendar entries, and the ``asesor`` demo login --
and leaves every real row alone. The generator itself is gone from the
repository, so this is the last piece of it: the way to clean up after it.

Two deliberate safety choices, because this runs against a *production*
database and there is no undo:

* It is a **dry run by default**. Without ``--yes`` it prints the database it
  is pointed at and what it would delete, and changes nothing. Naming the
  host matters: DATABASE_URL is easy to point at the wrong project, and
  "which database did I just wipe" is a bad question to ask afterwards.
* The delete runs in one transaction, so a failure halfway leaves the Inbox
  as it was rather than half-erased.

Without ``--demo-only`` this empties conversations and the contacts they
belong to, nothing else: tags, message templates, client lists, calendar
events and agents all survive. ``CalendarEvent.contact`` is SET_NULL, so
events outlive the contact they referenced.

``--demo-only`` is narrower in what it touches but wider in *kind*: it also
removes the demo calendar events and the ``asesor`` login, because the seed
generator created those too. One thing it deliberately leaves behind are the
five tags the generator invented (CLIENTE NUEVO, VENTA EFECTIVA, PRIMER
CONTACTO, SHOPIFY NUEVO, MAYORISTA EFECTIVA). By the time anyone runs this
they are ordinary tags: a real conversation may already wear one, the team
may have recolored them, and deleting a tag rewrites the history of every
conversation it was applied to -- which is the same reason ``Tag`` archives
instead of deleting. Retire them from the Etiquetas page if they are not
wanted.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from core.agents import is_app_user
from core.models import CalendarEvent, Client
from messaging.models import Conversation, ConversationTag, Message


#: What the old seed_conversations command stamped on everything it created,
#: kept verbatim: these are the keys --demo-only deletes by. The phone prefix
#: was reserved for fixtures (real Colombian numbers never start with it) and
#: covered the Estadísticas volume backdrop too (``+57300000009xx``).
DEMO_PHONE_PREFIX = "+5730000000"
DEMO_EVENT_DESCRIPTION = "Evento de demostración."
DEMO_EVENT_TITLES = [
    "Llamada de bienvenida a Camila",
    "Reunión semanal del equipo",
    "Seguimiento pedido #10432",
    "Llamada mayorista con Daniela",
    "Demo de catálogo para Valentina",
    "Confirmar recogida del cambio de talla",
    "Planeación de campaña de septiembre",
    "Cierre de caja de la semana",
]
DEMO_USERNAME = "asesor"


class Command(BaseCommand):
    help = (
        "Delete every conversation, message and contact -- an empty Inbox -- "
        "or, with --demo-only, just the seeded demo fixtures. "
        "Dry run unless --yes is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Actually delete. Without this the command only reports.",
        )
        parser.add_argument(
            "--keep-clients",
            action="store_true",
            help=(
                "Delete conversations and messages but keep the contact "
                "records, so client history survives an Inbox reset."
            ),
        )
        parser.add_argument(
            "--demo-only",
            action="store_true",
            help=(
                "Delete only the demo fixtures the old seed_conversations "
                "created (contacts +5730000000xx, their conversations and "
                "messages, the demo calendar events, the 'asesor' login). "
                "Real customers are untouched."
            ),
        )

    def handle(self, *args, **options):
        if options["demo_only"]:
            return self._handle_demo_only(confirmed=options["yes"])

        confirmed = options["yes"]
        keep_clients = options["keep_clients"]

        counts = {
            "mensajes": Message.objects.count(),
            "etiquetas de conversación": ConversationTag.objects.count(),
            "conversaciones": Conversation.objects.count(),
            "contactos": Client.objects.count(),
        }
        if keep_clients:
            counts.pop("contactos")

        # Name the target before touching it -- see the module docstring.
        db = connection.settings_dict
        target = db.get("HOST") or db.get("NAME")
        self.stdout.write(f"Base de datos: {db['ENGINE'].split('.')[-1]} · {target}")
        for label, count in counts.items():
            self.stdout.write(f"  {count:>6}  {label}")

        if not any(counts.values()):
            self.stdout.write(self.style.SUCCESS("Nada que borrar: el Inbox ya está vacío."))
            return

        if not confirmed:
            self.stdout.write(
                self.style.WARNING(
                    "\nSimulación: no se borró nada. Vuelve a correr con --yes "
                    "para borrarlo de verdad."
                )
            )
            return

        with transaction.atomic():
            # Explicit order rather than leaning on cascade, so the numbers
            # reported are the numbers actually deleted.
            Message.objects.all().delete()
            ConversationTag.objects.all().delete()
            Conversation.objects.all().delete()
            if not keep_clients:
                Client.objects.all().delete()

        self.stdout.write(self.style.SUCCESS("\nInbox vacío."))
        for label, count in counts.items():
            self.stdout.write(self.style.SUCCESS(f"  {count:>6}  {label} eliminados"))

    def _handle_demo_only(self, confirmed: bool) -> None:
        """The surgical wipe: only rows the seed generator stamped as its own.

        Contacts are the anchor -- conversations, messages and conversation
        tags cascade from them -- so the count reported for those is what
        the cascade will take. Calendar events do not cascade
        (``CalendarEvent.contact`` is SET_NULL) and are matched by their seed
        title *and* description, so a user's own "Reunión semanal del equipo"
        survives. The demo user goes last, and only when
        ``asesor`` is not a real teammate's account: every FK to it is
        SET_NULL, so deleting a name somebody actually uses would quietly
        erase their authorship everywhere instead of failing.
        """
        contacts = Client.objects.filter(phone__startswith=DEMO_PHONE_PREFIX)
        conversations = Conversation.objects.filter(contact__in=contacts)
        events = CalendarEvent.objects.filter(
            title__in=DEMO_EVENT_TITLES, description=DEMO_EVENT_DESCRIPTION
        )
        # ...unless somebody adopted the name as a real login. A teammate's
        # account is a non-staff row with a password of its own
        # (core.agents.is_app_user); the old generator's fixture was a staff
        # account, so /admin worked. Deleting a real account would silently
        # NULL their attribution on every conversation and message they ever
        # touched (all the FKs are SET_NULL), with no undo.
        demo_users = get_user_model().objects.filter(username=DEMO_USERNAME)
        if any(is_app_user(user) for user in demo_users):
            demo_users = demo_users.none()
            self.stdout.write(
                self.style.WARNING(
                    f"  (el usuario {DEMO_USERNAME!r} es una cuenta real del "
                    f"equipo: no se toca)"
                )
            )

        counts = {
            "mensajes": Message.objects.filter(conversation__in=conversations).count(),
            "etiquetas de conversación": ConversationTag.objects.filter(
                conversation__in=conversations
            ).count(),
            "conversaciones": conversations.count(),
            "contactos de demo": contacts.count(),
            "eventos de demo": events.count(),
            "usuario 'asesor'": demo_users.count(),
        }

        db = connection.settings_dict
        target = db.get("HOST") or db.get("NAME")
        self.stdout.write(f"Base de datos: {db['ENGINE'].split('.')[-1]} · {target}")
        self.stdout.write("Solo datos de demostración (los clientes reales no se tocan):")
        for label, count in counts.items():
            self.stdout.write(f"  {count:>6}  {label}")

        if not any(counts.values()):
            self.stdout.write(self.style.SUCCESS("Nada que borrar: no hay datos de demo."))
            return

        if not confirmed:
            self.stdout.write(
                self.style.WARNING(
                    "\nSimulación: no se borró nada. Vuelve a correr con --yes "
                    "para borrarlo de verdad."
                )
            )
            return

        with transaction.atomic():
            contacts.delete()  # conversations, messages and tags cascade
            events.delete()
            demo_users.delete()

        self.stdout.write(self.style.SUCCESS("\nDatos de demo eliminados."))
        for label, count in counts.items():
            self.stdout.write(self.style.SUCCESS(f"  {count:>6}  {label} eliminados"))
        # Said out loud rather than left as a silent gap -- see the docstring.
        self.stdout.write(
            "\nLas etiquetas que creó el generador (CLIENTE NUEVO, VENTA "
            "EFECTIVA, ...) se conservan: puede que ya estén aplicadas a "
            "conversaciones reales. Archívalas desde Etiquetas si no las quieres."
        )
