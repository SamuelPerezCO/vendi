"""Delete every user row -- the clean slate before rebuilding the team.

Written for the moment when the login list has accumulated leftovers (demo
fixtures, a teammate who left, duplicate accounts) and you want the database
to hold exactly the people you are about to configure and nobody else.

The database owns every login (see ``core.agents``), so deleting a row is
deleting that person's access -- permanent, which is why the Usuarios page
deliberately offers deactivation instead. Afterwards ``manage.py
crear_maestro`` is how the first master gets back in. It is destructive in a
second way too; see below.

**Deleting a row erases attribution, everywhere.** Every FK to a user is
``on_delete=SET_NULL``, so nothing cascades -- no conversation, message or
calendar event is lost -- but each of these silently becomes NULL:

    Conversation.assigned_to   Message.sent_by      ConversationTag.tagged_by
    CalendarEvent.assigned_to  CalendarEvent.created_by
    QuickReply.created_by      Tag.created_by

The history survives; the answer to "who handled this" does not. That applies
even to a username you are re-creating afterwards: the new row is a new id,
and the old rows are already nulled. The dry run counts these before you
commit, because the number is the whole decision.

Two deliberate safety choices, matching ``reset_conversations`` -- this runs
against a *production* database and there is no undo:

* It is a **dry run by default**. Without ``--yes`` it prints the database it
  is pointed at, every user it would delete, and how many references it would
  null, and changes nothing.
* The delete runs in one transaction, so a failure halfway leaves the user
  list as it was rather than half-erased.

Django admin accounts (``is_staff``/``is_superuser``) are **kept** unless
``--include-staff`` is passed. Those are /admin logins this CRM does not
manage (see ``core.agents._is_app_user``), and they are one escape hatch
that gets you back in after a wipe (``manage.py crear_maestro`` is the
other). Removing them by accident while re-seeding the team is how a
deployment locks itself out completely.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from core.models import CalendarEvent, QuickReply
from messaging.models import Conversation, ConversationTag, Message, Tag


#: Every FK to AUTH_USER_MODEL, as (label, model, field). All are SET_NULL, so
#: this is the list of attributions a delete quietly blanks -- named here so
#: the dry run can count them rather than describe them vaguely.
USER_REFERENCES = [
    ("conversaciones asignadas", Conversation, "assigned_to"),
    ("mensajes enviados", Message, "sent_by"),
    ("etiquetas aplicadas", ConversationTag, "tagged_by"),
    ("eventos asignados", CalendarEvent, "assigned_to"),
    ("eventos creados", CalendarEvent, "created_by"),
    ("respuestas rápidas creadas", QuickReply, "created_by"),
    ("etiquetas creadas", Tag, "created_by"),
]


class Command(BaseCommand):
    help = (
        "Borra todas las filas de usuario -- la pizarra limpia antes de "
        "reconfigurar el equipo. Las cuentas de Django admin se conservan "
        "salvo --include-staff. Simulación salvo que se pase --yes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Borrar de verdad. Sin esto el comando solo informa.",
        )
        parser.add_argument(
            "--include-staff",
            action="store_true",
            help=(
                "Borrar también las cuentas de Django admin (is_staff / "
                "is_superuser). Son una vía de entrada si el equipo queda "
                "fuera; por defecto se conservan."
            ),
        )

    def handle(self, *args, **options):
        User = get_user_model()
        confirmed = options["yes"]
        include_staff = options["include_staff"]

        users = User.objects.all().order_by("username")
        if not include_staff:
            kept_staff = users.filter(is_staff=True) | users.filter(is_superuser=True)
            kept_staff = kept_staff.distinct()
            users = users.exclude(is_staff=True).exclude(is_superuser=True)
        else:
            kept_staff = User.objects.none()

        # Name the target before touching it -- see the module docstring.
        db = connection.settings_dict
        target = db.get("HOST") or db.get("NAME")
        self.stdout.write(f"Base de datos: {db['ENGINE'].split('.')[-1]} · {target}")

        doomed = list(users)

        if not doomed:
            self.stdout.write(self.style.SUCCESS("Nada que borrar: no hay usuarios."))
            self._report_kept(kept_staff)
            return

        self.stdout.write(f"\nSe borrarían {len(doomed)} usuario(s):")
        for user in doomed:
            marks = []
            if not user.is_active:
                marks.append("inactivo")
            suffix = f"  ({'; '.join(marks)})" if marks else ""
            self.stdout.write(f"  {user.username}{suffix}")

        self._report_kept(kept_staff)

        ids = [user.pk for user in doomed]
        self.stdout.write("\nReferencias que quedarían en NULL (SET_NULL, no se borra nada):")
        total = 0
        for label, model, field in USER_REFERENCES:
            count = model.objects.filter(**{f"{field}__in": ids}).count()
            total += count
            self.stdout.write(f"  {count:>6}  {label}")
        if total:
            self.stdout.write(
                self.style.WARNING(
                    f"  {total} atribuciones se pierden y no se pueden recuperar."
                )
            )

        if not confirmed:
            self.stdout.write(
                self.style.WARNING(
                    "\nSimulación: no se borró nada. Vuelve a correr con --yes "
                    "para borrarlo de verdad."
                )
            )
            return

        with transaction.atomic():
            deleted, _ = User.objects.filter(pk__in=ids).delete()

        self.stdout.write(self.style.SUCCESS(f"\n{len(ids)} usuario(s) eliminados."))
        self.stdout.write(
            "Para volver a entrar, crea el primer maestro con manage.py "
            "crear_maestro; el resto del equipo se crea desde CRM > Equipo > Usuarios."
        )

    def _report_kept(self, kept_staff) -> None:
        """Say out loud which rows were spared, so 'it did nothing' is never
        a mystery -- a staff-only database is the common case here."""
        names = list(kept_staff.values_list("username", flat=True))
        if names:
            self.stdout.write(
                f"\nSe conservan {len(names)} cuenta(s) de Django admin: "
                f"{', '.join(names)}. Usa --include-staff para borrarlas también."
            )
