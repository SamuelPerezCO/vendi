"""Empty the CRM for launch: delete everything except the team.

``reset_conversations`` clears the Inbox, and its ``--demo-only`` removes
just what the old generator stamped. This clears the *app*: contacts and
their conversations, but also the tags, the calendar events, the client
lists, the catalog, the ``asesor`` fixture account and every other row left
over from building the thing, whatever created them. What survives is the
team -- the accounts that can log in and be assigned work -- so the first
real customer message arrives into a CRM that contains nothing but itself.

Use ``reset_conversations --demo-only`` instead when the database already
holds real customers: it deletes by the fixtures' markers and leaves
everything else alone. This command makes no such distinction -- it empties
the app.

Run it once, when the WhatsApp number goes live::

    python manage.py go_live          # report only
    python manage.py go_live --yes    # actually delete

Same two safety choices as ``reset_conversations``, for the same reason (this
runs against production and there is no undo):

* **Dry run by default.** Without ``--yes`` it names the database it is
  pointed at, prints what it would delete, and changes nothing.
* **One transaction**, so a failure halfway leaves the database as it was
  rather than half-erased.

Who counts as "the team", and therefore survives:

* anyone holding a real password (:func:`core.agents.is_app_user`): created
  from CRM > Equipo > Usuarios or with ``manage.py crear_maestro``
* any Django superuser

Every other ``User`` row is an assignee-only row -- the ``asesor`` fixture,
or a name a seed script invented -- and goes. Deactivated teammates are kept:
deactivation is the Usuarios page's version of deleting, a decision it already
recorded, and their name still belongs on the record of who did what.

The catalog goes too -- products, WhatsApp templates and quick replies --
since on a CRM that has never been live it can only be trial data.
``--keep-catalog`` keeps all three, for the case where the templates are
already approved by Meta or the quick replies are the real ones.

What this does **not** touch: uploaded media already in Vercel Blob (the rows
pointing at it are deleted, the files stay).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from core import agents
from core.models import (
    CalendarEvent,
    Client,
    ClientList,
    MessageTemplate,
    Product,
    QuickReply,
)
from messaging.management.commands.reset_conversations import DEMO_USERNAME
from messaging.models import Conversation, ConversationTag, Message, Tag


class Command(BaseCommand):
    help = (
        "Delete every conversation, contact, tag, event and fixture account -- "
        "an empty CRM ready for real customers. Dry run unless --yes is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Actually delete. Without this the command only reports.",
        )
        parser.add_argument(
            "--keep-catalog",
            action="store_true",
            help=(
                "Keep products, message templates and quick replies (e.g. "
                "WhatsApp templates already approved by Meta). Everything "
                "else still goes."
            ),
        )

    def handle(self, *args, **options):
        confirmed = options["yes"]
        keep_catalog = options["keep_catalog"]

        User = get_user_model()
        # Resolved before anything is deleted: `keep` is what defines the
        # survivors, and after the delete the queryset would be evaluated
        # against rows that no longer exist.
        keep, drop = _split_team(User.objects.all())

        counts = {
            "mensajes": Message.objects.count(),
            "etiquetas aplicadas": ConversationTag.objects.count(),
            "conversaciones": Conversation.objects.count(),
            "contactos": Client.objects.count(),
            "etiquetas": Tag.objects.count(),
            "eventos de calendario": CalendarEvent.objects.count(),
            "listas de clientes": ClientList.objects.count(),
        }
        if not keep_catalog:
            counts["productos"] = Product.objects.count()
            counts["plantillas"] = MessageTemplate.objects.count()
            counts["respuestas rápidas"] = QuickReply.objects.count()
        counts["cuentas de prueba"] = len(drop)

        # Name the target before touching it: DATABASE_URL is easy to point at
        # the wrong project, and "which database did I just wipe" is a bad
        # question to ask afterwards.
        db = connection.settings_dict
        target = db.get("HOST") or db.get("NAME")
        self.stdout.write(f"Base de datos: {db['ENGINE'].split('.')[-1]} · {target}")
        self.stdout.write("")
        for label, count in counts.items():
            self.stdout.write(f"  {count:>6}  {label}")

        self.stdout.write("")
        if keep:
            noun = "cuenta" if len(keep) == 1 else "cuentas"
            self.stdout.write(f"Se conserva{'' if len(keep) == 1 else 'n'} "
                              f"{len(keep)} {noun} del equipo:")
            for user in keep:
                name = user.get_full_name() or user.username
                flags = []
                if agents.is_master(user):
                    flags.append("maestro")
                if not user.is_active:
                    flags.append("desactivada")
                suffix = f" ({', '.join(flags)})" if flags else ""
                self.stdout.write(f"    · {name} [{user.username}]{suffix}")
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Ninguna cuenta del equipo sobrevive: nadie tiene contraseña "
                    "propia ni es superusuario. Crea tu cuenta con manage.py "
                    "crear_maestro antes de correr esto con --yes, o te quedas fuera."
                )
            )
        if keep_catalog:
            self.stdout.write(
                f"Se conservan {Product.objects.count()} productos, "
                f"{MessageTemplate.objects.count()} plantillas y "
                f"{QuickReply.objects.count()} respuestas rápidas (--keep-catalog)."
            )

        if not any(counts.values()):
            self.stdout.write(self.style.SUCCESS("\nNada que borrar: el CRM ya está vacío."))
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
            # reported are the numbers actually deleted. Accounts go last:
            # every FK pointing at them (assigned_to, sent_by, created_by,
            # tagged_by) is SET_NULL, and by now those rows are gone anyway.
            Message.objects.all().delete()
            ConversationTag.objects.all().delete()
            Conversation.objects.all().delete()
            Client.objects.all().delete()
            Tag.objects.all().delete()
            CalendarEvent.objects.all().delete()
            ClientList.objects.all().delete()
            if not keep_catalog:
                Product.objects.all().delete()
                MessageTemplate.objects.all().delete()
                QuickReply.objects.all().delete()
            if drop:
                User.objects.filter(pk__in=[u.pk for u in drop]).delete()

        self.stdout.write(self.style.SUCCESS("\nCRM vacío. Listo para clientes reales."))
        self.stdout.write(self.style.SUCCESS("Se eliminaron:"))
        for label, count in counts.items():
            if count:
                self.stdout.write(self.style.SUCCESS(f"  {count:>6}  {label}"))
        self.stdout.write(
            "\nNota: los archivos ya subidos siguen en Vercel Blob; esto borra "
            "las filas que apuntaban a ellos, no los archivos."
        )


def _split_team(users) -> tuple[list, list]:
    """Split ``users`` into (team, fixtures).

    A row is the team's if it holds a real password (CRM > Equipo > Usuarios
    or crear_maestro created it) or if it is a Django superuser. Anything else exists only to be pointed at by
    ``assigned_to``: a name a seed script invented.

    The demo advisor is named explicitly rather than inferred.
    :func:`core.agents.is_app_user` reads "has a real password" as "a person
    created this account", which is true of the Usuarios page but not of the
    old generator: it gave ``asesor`` a password too (so /admin worked out of
    the box), and without this that fixture would survive the very purge meant
    to remove it. A real teammate who happens to be called ``asesor`` is
    listed under "cuentas de prueba" in the dry run, which is the moment to
    notice and rename them.
    """
    keep, drop = [], []
    for user in users:
        is_team = user.username != DEMO_USERNAME and (
            user.is_superuser
            # Named here rather than left to is_app_user, which excludes staff
            # on purpose (a /admin account is not the Usuarios page's to hand
            # out). Right for a screen that resets passwords, wrong for a
            # command that deletes rows: somebody with /admin access is a
            # colleague, whatever this CRM thinks of them.
            or user.is_staff
            or agents.is_app_user(user)
        )
        (keep if is_team else drop).append(user)
    keep.sort(key=_display_key)
    drop.sort(key=_display_key)
    return keep, drop


def _display_key(user) -> str:
    """Case-insensitive sort on what the UI shows for a user."""
    return (user.get_full_name() or user.username).casefold()
