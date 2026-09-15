"""The reset_conversations command: it must not delete without --yes."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import CalendarEvent, Client
from messaging.models import Conversation, Message, Tag
from messaging.providers.types import MessageStatus


class ResetConversationsTests(TestCase):
    def setUp(self):
        self.contact = Client.objects.create(
            first_name="Cliente", last_name="Prueba", phone="+573000000099"
        )
        self.conversation = Conversation.objects.create(
            contact=self.contact, channel="whatsapp"
        )
        Message.objects.create(
            conversation=self.conversation,
            direction=Message.INBOUND,
            body="hola",
            status=MessageStatus.DELIVERED.value,
            provider_message_id="wamid.RESET1",
            timestamp=timezone.now(),
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("reset_conversations", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_deletes_nothing(self):
        """The default must be harmless: this runs against production."""
        output = self.run_command()

        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)
        self.assertIn("Simulación", output)

    def test_dry_run_names_the_database_it_would_touch(self):
        """A wipe pointed at the wrong DATABASE_URL is unrecoverable, so the
        target has to be visible before you type --yes."""
        self.assertIn("Base de datos:", self.run_command())

    def test_yes_empties_the_inbox(self):
        self.run_command("--yes")

        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    def test_keep_clients_preserves_contacts(self):
        self.run_command("--yes", "--keep-clients")

        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 1)

    def test_tags_survive(self):
        """Tags are configuration, not conversation data."""
        Tag.objects.create(name="Primer contacto")

        self.run_command("--yes")

        self.assertEqual(Tag.objects.count(), 1)

    def test_calendar_events_survive_their_contact(self):
        """CalendarEvent.contact is SET_NULL -- the event outlives the client."""
        event = CalendarEvent.objects.create(
            title="Llamada",
            start=timezone.now(),
            end=timezone.now(),
            contact=self.contact,
        )

        self.run_command("--yes")

        event.refresh_from_db()
        self.assertIsNone(event.contact)

    def test_empty_inbox_is_a_no_op(self):
        self.run_command("--yes")
        output = self.run_command("--yes")

        self.assertIn("ya está vacío", output)


class DemoOnlyResetTests(TestCase):
    """--demo-only: the surgical wipe for a database that already holds real
    customers. Everything the old seed generator stamped goes; nothing else."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        # A real customer (any phone outside the reserved prefix) ...
        self.real = Client.objects.create(
            first_name="Ana", last_name="Real", phone="+573167687288"
        )
        self.real_chat = Conversation.objects.create(contact=self.real, channel="whatsapp")
        Message.objects.create(
            conversation=self.real_chat, direction=Message.INBOUND, body="hola",
            status=MessageStatus.DELIVERED.value, provider_message_id="wamid.REAL1",
            timestamp=timezone.now(),
        )
        # ... a seeded one, in the reserved range, plus a volume-backdrop one.
        self.demo = Client.objects.create(
            first_name="Camila", last_name="Pruebas", phone="+573000000001"
        )
        demo_chat = Conversation.objects.create(contact=self.demo, channel="whatsapp")
        Message.objects.create(
            conversation=demo_chat, direction=Message.INBOUND, body="demo",
            status=MessageStatus.DELIVERED.value, provider_message_id="wamid.DEMO1",
            timestamp=timezone.now(),
        )
        # The volume backdrop used prefix + "9xx": seven zeros, then the 9.
        Client.objects.create(first_name="Vol", last_name="Backdrop", phone="+5730000000901")
        # Demo events vs. a user's own event that happens to share a title.
        CalendarEvent.objects.create(
            title="Reunión semanal del equipo", description="Evento de demostración.",
            start=timezone.now(), end=timezone.now(),
        )
        self.own_event = CalendarEvent.objects.create(
            title="Reunión semanal del equipo", description="La de verdad.",
            start=timezone.now(), end=timezone.now(),
        )
        User = get_user_model()
        # Exactly what the removed seed_conversations created: a staff account
        # (so /admin worked) with the printed demo password.
        User.objects.create_user(
            "asesor", password="asesor123", first_name="Asesor", last_name="Demo",
            is_staff=True,
        )
        self.real_user = User.objects.create_user("samuel", password="x" * 12)

    def run_command(self, *args):
        out = StringIO()
        call_command("reset_conversations", "--demo-only", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_and_deletes_nothing(self):
        output = self.run_command()
        self.assertIn("Simulación", output)
        self.assertIn("Solo datos de demostración", output)
        self.assertEqual(Client.objects.count(), 3)
        self.assertEqual(CalendarEvent.objects.count(), 2)

    def test_yes_removes_only_the_demo_fixtures(self):
        from django.contrib.auth import get_user_model

        self.run_command("--yes")

        # Real customer, conversation and message survive.
        self.assertTrue(Client.objects.filter(pk=self.real.pk).exists())
        self.assertTrue(Conversation.objects.filter(pk=self.real_chat.pk).exists())
        self.assertEqual(Message.objects.filter(conversation=self.real_chat).count(), 1)
        # Every reserved-range contact -- and everything hanging off it -- is gone.
        self.assertFalse(Client.objects.filter(phone__startswith="+5730000000").exists())
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)
        # The demo event went; the user's own, same-titled one did not.
        self.assertEqual(CalendarEvent.objects.count(), 1)
        self.assertTrue(CalendarEvent.objects.filter(pk=self.own_event.pk).exists())
        # The demo login went; the real agent stayed.
        User = get_user_model()
        self.assertFalse(User.objects.filter(username="asesor").exists())
        self.assertTrue(User.objects.filter(pk=self.real_user.pk).exists())

    def test_tags_survive(self):
        Tag.objects.create(name="CLIENTE NUEVO")
        self.run_command("--yes")
        self.assertEqual(Tag.objects.count(), 1)

    def test_nothing_to_delete_is_a_no_op(self):
        self.run_command("--yes")
        output = self.run_command("--yes")
        self.assertIn("no hay datos de demo", output)
        self.assertEqual(
            Client.objects.count(), 1, list(Client.objects.values_list("first_name", "phone"))
        )


class DemoUserIsProtectedWhenRealTests(TestCase):
    """'asesor' is a plausible username for an actual advisor. Every FK to a
    user is SET_NULL, so deleting one that someone uses does not fail loudly
    -- it silently erases their authorship everywhere."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user("asesor", password="x" * 12)
        contact = Client.objects.create(
            first_name="Ana", last_name="Real", phone="+573167687288"
        )
        self.chat = Conversation.objects.create(
            contact=contact, channel="whatsapp", assigned_to=self.user
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("reset_conversations", "--demo-only", *args, stdout=out)
        return out.getvalue()

    def test_a_real_teammate_named_asesor_is_left_alone(self):
        """A non-staff account with a password of its own is somebody's
        login (core.agents.is_app_user), whatever it is called."""
        from django.contrib.auth import get_user_model

        output = self.run_command("--yes")

        self.assertIn("cuenta real", output)
        self.assertTrue(get_user_model().objects.filter(username="asesor").exists())
        self.chat.refresh_from_db()
        self.assertEqual(self.chat.assigned_to, self.user)

    def test_a_password_less_asesor_is_a_fixture_and_goes(self):
        from django.contrib.auth import get_user_model

        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        self.run_command("--yes")
        self.assertFalse(get_user_model().objects.filter(username="asesor").exists())

    def test_the_leftover_demo_login_still_goes_when_nobody_uses_it(self):
        from django.contrib.auth import get_user_model

        # The old generator's fixture: a staff account, so /admin worked.
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.run_command("--yes")

        self.assertFalse(get_user_model().objects.filter(username="asesor").exists())
