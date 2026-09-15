"""Tests for ``go_live``: the clean slate that keeps the team.

The one thing that must hold is that it empties the CRM but never the
accounts -- losing those would lock everyone out of the app they just
cleaned. The narrower ``reset_conversations --demo-only`` is covered by
messaging/tests_reset.py, and the webhook that must not answer on a real
deployment by messaging/tests_webhook_hardening.py.
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core import agents
from core.models import (
    CalendarEvent,
    Client,
    ClientList,
    MessageTemplate,
    Product,
    QuickReply,
)
from messaging.models import Conversation, ConversationTag, Message, Tag

def run(*args) -> str:
    out = StringIO()
    call_command("go_live", *args, stdout=out)
    return out.getvalue()


class GoLiveTests(TestCase):
    """The purge itself."""

    def setUp(self):
        User = get_user_model()

        # The team: one login created the way CRM > Equipo > Usuarios does.
        self.teammate = agents.create_user(
            "samuel", "una-clave-larga", "Samuel Pérez", master=True
        )

        # A fixture account: assignable, no way in. This is what a seed leaves.
        self.fixture = User.objects.create(
            username="asesor", first_name="Asesor", last_name="Demo"
        )
        self.fixture.set_unusable_password()
        self.fixture.save()

        self.contact = Client.objects.create(
            first_name="Camila", last_name="Pruebas", phone="+573000000001",
            channel="whatsapp",
        )
        self.conversation = Conversation.objects.create(
            contact=self.contact, channel="whatsapp", assigned_to=self.fixture,
            last_inbound_at=timezone.now(),
        )
        Message.objects.create(
            conversation=self.conversation, direction=Message.INBOUND,
            body="Hola", provider_message_id="seed-1", timestamp=timezone.now(),
        )
        self.tag = Tag.objects.create(name="CLIENTE NUEVO", color="yellow",
                                      created_by=self.fixture)
        ConversationTag.objects.create(conversation=self.conversation, tag=self.tag,
                                       tagged_by=self.fixture)
        CalendarEvent.objects.create(
            title="Llamada de bienvenida a Camila",
            description="Evento de demostración.",
            start=timezone.now(), end=timezone.now(),
            assigned_to=self.fixture, created_by=self.fixture,
        )
        ClientList.objects.create(name="Lista de prueba")
        Product.objects.create(name="Camiseta", price=50000)
        MessageTemplate.objects.create(name="bienvenida", body="Hola {{1}}")
        QuickReply.objects.create(title="Horario", body="De 9 a 6.")

    def test_dry_run_changes_nothing(self):
        output = run()
        self.assertIn("Simulación", output)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_dry_run_reports_what_it_would_delete(self):
        output = run()
        for label in ("mensajes", "conversaciones", "contactos", "etiquetas",
                      "eventos de calendario", "cuentas de prueba"):
            self.assertIn(label, output)

    def test_yes_empties_the_crm(self):
        run("--yes")
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(ConversationTag.objects.count(), 0)
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(CalendarEvent.objects.count(), 0)
        self.assertEqual(ClientList.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertEqual(QuickReply.objects.count(), 0)

    def test_the_team_survives_and_the_fixtures_do_not(self):
        run("--yes")
        usernames = list(get_user_model().objects.values_list("username", flat=True))
        self.assertEqual(usernames, ["samuel"])

    def test_the_seeded_advisor_goes_even_with_a_password(self):
        """The old generator gave `asesor` a real password so /admin worked,
        which would otherwise read as "a person created this account" and keep
        the fixture through the purge meant to remove it."""
        self.fixture.set_password("asesor123")
        self.fixture.save(update_fields=["password"])
        run("--yes")
        self.assertFalse(get_user_model().objects.filter(username="asesor").exists())

    def test_the_surviving_login_still_works(self):
        """The whole point: whoever runs this can still get in afterwards."""
        run("--yes")
        self.assertIsNotNone(agents.authenticate("samuel", "una-clave-larga"))

    def test_a_deactivated_teammate_is_kept(self):
        self.teammate.is_active = False
        self.teammate.save(update_fields=["is_active"])
        run("--yes")
        self.assertTrue(get_user_model().objects.filter(username="samuel").exists())

    def test_a_superuser_is_kept(self):
        get_user_model().objects.create_superuser("root", password="x" * 12)
        run("--yes")
        self.assertTrue(get_user_model().objects.filter(username="root").exists())

    def test_keep_catalog_leaves_the_catalog(self):
        run("--yes", "--keep-catalog")
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(MessageTemplate.objects.count(), 1)
        self.assertEqual(QuickReply.objects.count(), 1)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_names_the_database_before_touching_it(self):
        self.assertIn("Base de datos:", run())

    def test_lists_the_accounts_it_keeps(self):
        self.assertIn("samuel", run())

    def test_running_twice_is_harmless(self):
        run("--yes")
        output = run("--yes")
        self.assertIn("ya está vacío", output)

    def test_warns_when_no_account_would_survive(self):
        """A real password is what marks a row as a login (agents.is_app_user),
        so a team with none of those is a team nothing would keep."""
        self.teammate.set_unusable_password()
        self.teammate.save(update_fields=["password"])
        self.assertIn("Ninguna cuenta del equipo sobrevive", run())


class StaffAccountsSurviveTests(TestCase):
    """core.agents.is_app_user excludes Django staff (a /admin account is not
    the Usuarios page's to manage). go_live must not read that exclusion as
    "delete them" -- it deletes rows, and /admin access means colleague."""

    def test_a_staff_only_account_is_kept(self):
        User = get_user_model()
        staff = User.objects.create_user("adminweb", password="clave-larga")
        staff.is_staff = True
        staff.save()
        self.assertFalse(agents.is_app_user(staff))   # not a Usuarios row...
        call_command("go_live", "--yes", stdout=StringIO())
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())   # ...but kept

    def test_a_superuser_is_kept(self):
        User = get_user_model()
        root = User.objects.create_superuser("root", password="clave-larga")
        call_command("go_live", "--yes", stdout=StringIO())
        self.assertTrue(User.objects.filter(pk=root.pk).exists())
