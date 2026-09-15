"""Tests for the three messaging automations under Configuración de
mensajería -- not to be confused with core/tests_automatizaciones.py, which
covers the (currently unmounted) Automatizaciones chatbot screen.

These cover: the welcome message, round-robin
assignment, and the WhatsApp widget.

The two that fire on inbound traffic are tested through
messaging.services.process_inbound_events -- the real webhook path -- rather
than by calling them directly, because "does it fire, exactly once, without
costing the customer's message" is the whole question.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import agents
from core.models import Client, MessagingSettings
from messaging import services
from messaging.models import Conversation, Message
from messaging.providers.fake import FakeProvider
from messaging.providers.types import InboundEvent



def inbound(phone="+573001112233", body="hola", mid=None):
    """One inbound message, through the real webhook path."""
    services.process_inbound_events([
        InboundEvent(
            event_type="message",
            provider_message_id=mid or f"in-{phone}-{timezone.now().timestamp()}",
            from_number=phone, body=body, channel="whatsapp",
        )
    ])


def settings_row(**fields):
    row = MessagingSettings.load()
    for k, v in fields.items():
        setattr(row, k, v)
    row.save()
    return row


class WithAgentsTestCase(TestCase):
    """Ana and Beto, the agents the rotation goes through -- database users,
    since logins no longer come from the environment."""

    def setUp(self):
        super().setUp()
        agents.create_user("Ana", "clave-larga-a", "Ana")
        agents.create_user("Beto", "clave-larga-b", "Beto")


class WelcomeMessageTests(TestCase):
    def test_off_by_default_nothing_is_sent(self):
        inbound()
        self.assertEqual(Message.objects.filter(direction="outbound").count(), 0)

    def test_it_greets_the_first_time_someone_writes(self):
        settings_row(welcome_enabled=True, welcome_body="¡Hola! Ya te leemos.")
        inbound()
        out = Message.objects.filter(direction="outbound")
        self.assertEqual(out.count(), 1)
        self.assertEqual(out.first().body, "¡Hola! Ya te leemos.")

    def test_it_greets_only_once_ever(self):
        settings_row(welcome_enabled=True, welcome_body="Hola")
        inbound(mid="a")
        inbound(mid="b")
        inbound(mid="c")
        self.assertEqual(Message.objects.filter(direction="outbound").count(), 1)

    def test_a_second_conversation_months_later_is_not_greeted_again(self):
        settings_row(welcome_enabled=True, welcome_body="Hola")
        inbound(mid="a")
        Conversation.objects.update(status=Conversation.RESOLVED)
        inbound(mid="b")   # a brand-new thread for the same person
        self.assertEqual(Conversation.objects.count(), 2)
        self.assertEqual(Message.objects.filter(direction="outbound").count(), 1)

    def test_a_different_person_gets_their_own_greeting(self):
        settings_row(welcome_enabled=True, welcome_body="Hola")
        inbound(phone="+573001112233", mid="a")
        inbound(phone="+573009998877", mid="b")
        self.assertEqual(Message.objects.filter(direction="outbound").count(), 2)

    def test_enabled_with_no_text_sends_nothing(self):
        settings_row(welcome_enabled=True, welcome_body="   ")
        inbound()
        self.assertEqual(Message.objects.filter(direction="outbound").count(), 0)

    def test_the_customers_message_survives_a_failing_greeting(self):
        # The greeting is best-effort; losing it must never lose their message.
        settings_row(welcome_enabled=True, welcome_body="Hola")
        with mock.patch.object(FakeProvider, "send_text", side_effect=RuntimeError("down")):
            inbound()
        self.assertEqual(Message.objects.filter(direction="inbound").count(), 1)

    def test_a_failed_greeting_is_retried_on_the_next_message(self):
        # welcomed_at is handed back, so the turn is not silently burned.
        settings_row(welcome_enabled=True, welcome_body="Hola")
        with mock.patch.object(FakeProvider, "send_text", side_effect=RuntimeError("down")):
            inbound(mid="a")
        self.assertIsNone(Client.objects.get().welcomed_at)

        inbound(mid="b")
        out = Message.objects.filter(direction="outbound")
        # Two rows, and that is correct: send_message keeps the failed attempt
        # marked `failed` so the thread shows what happened, rather than
        # deleting the evidence. Exactly one of them actually went out.
        self.assertEqual(out.count(), 2)
        self.assertEqual(out.filter(status="failed").count(), 1)
        self.assertEqual(out.exclude(status="failed").count(), 1)
        self.assertIsNotNone(Client.objects.get().welcomed_at)

    def test_the_stamp_is_what_makes_it_once_only(self):
        settings_row(welcome_enabled=True, welcome_body="Hola")
        inbound()
        self.assertIsNotNone(Client.objects.get().welcomed_at)


class AutoAssignTests(WithAgentsTestCase):
    def test_off_by_default_conversations_stay_unassigned(self):
        inbound()
        self.assertIsNone(Conversation.objects.get().assigned_to)

    def test_it_rotates_through_the_agents(self):
        settings_row(assign_enabled=True)
        inbound(phone="+573000000001", mid="a")
        inbound(phone="+573000000002", mid="b")
        inbound(phone="+573000000003", mid="c")
        got = [c.assigned_to.username for c in Conversation.objects.order_by("id")]
        self.assertEqual(got, ["Ana", "Beto", "Ana"])   # wraps around

    def test_the_cursor_survives_between_messages(self):
        settings_row(assign_enabled=True)
        inbound(phone="+573000000001", mid="a")
        self.assertEqual(MessagingSettings.load().assign_cursor, 1)

    def test_an_already_owned_conversation_is_never_reassigned(self):
        settings_row(assign_enabled=True)
        inbound(mid="a")
        first = Conversation.objects.get()
        inbound(mid="b")
        first.refresh_from_db()
        self.assertEqual(first.assigned_to.username, "Ana")   # unchanged

    def test_with_nobody_to_assign_to_it_leaves_the_chat_alone(self):
        get_user_model().objects.all().delete()
        settings_row(assign_enabled=True)
        inbound()
        self.assertIsNone(Conversation.objects.get().assigned_to)

    def test_the_customers_message_survives_a_failing_assignment(self):
        settings_row(assign_enabled=True)
        with mock.patch("core.agents.agent_users", side_effect=RuntimeError("boom")):
            inbound()
        self.assertEqual(Message.objects.filter(direction="inbound").count(), 1)


class BienvenidaScreenTests(TestCase):
    URL = reverse("bienvenida_save")
    PAGE = reverse("section", args=["mensajeria"]) + "?view=mensajes-bienvenida"

    def test_the_page_is_real_now(self):
        response = self.client.get(self.PAGE)
        self.assertContains(response, "Mensajes de bienvenida")
        self.assertNotContains(response, "próximamente")

    def test_saving_turns_it_on(self):
        self.client.post(self.URL, {"welcome_enabled": "1", "welcome_body": "Hola"})
        row = MessagingSettings.load()
        self.assertTrue(row.welcome_enabled)
        self.assertEqual(row.welcome_body, "Hola")

    def test_enabling_with_no_text_is_refused(self):
        html = self.client.post(self.URL, {"welcome_enabled": "1", "welcome_body": " "}).content.decode()
        self.assertIn("Escribe el mensaje antes de activarlo", html)
        self.assertFalse(MessagingSettings.load().welcome_enabled)

    def test_it_can_be_turned_off_without_losing_the_text(self):
        self.client.post(self.URL, {"welcome_enabled": "1", "welcome_body": "Hola"})
        self.client.post(self.URL, {"welcome_body": "Hola"})   # unchecked
        row = MessagingSettings.load()
        self.assertFalse(row.welcome_enabled)
        self.assertEqual(row.welcome_body, "Hola")


class AsignacionScreenTests(WithAgentsTestCase):
    URL = reverse("asignacion_save")
    PAGE = reverse("section", args=["mensajeria"]) + "?view=asignacion-automatica"

    def test_the_page_lists_the_rotation(self):
        html = self.client.get(self.PAGE).content.decode()
        self.assertIn("Asignación automática", html)
        self.assertIn("Ana", html)
        self.assertIn("Beto", html)
        self.assertNotIn("próximamente", html)

    def test_turning_it_on_and_off(self):
        self.client.post(self.URL, {"assign_enabled": "1"})
        self.assertTrue(MessagingSettings.load().assign_enabled)
        self.client.post(self.URL, {})
        self.assertFalse(MessagingSettings.load().assign_enabled)

    def test_enabling_with_no_agents_is_refused(self):
        get_user_model().objects.all().delete()
        html = self.client.post(self.URL, {"assign_enabled": "1"}).content.decode()
        self.assertIn("No hay agentes", html)
        self.assertFalse(MessagingSettings.load().assign_enabled)

    def test_it_marks_whose_turn_is_next(self):
        self.client.post(self.URL, {"assign_enabled": "1"})
        self.assertIn("Le toca", self.client.get(self.PAGE).content.decode())


class WidgetScreenTests(TestCase):
    URL = reverse("widget_save")
    PAGE = reverse("section", args=["mensajeria"]) + "?view=widget-whatsapp"

    def test_the_page_is_real_now(self):
        response = self.client.get(self.PAGE)
        self.assertContains(response, "Widget de WhatsApp")
        self.assertNotContains(response, "próximamente")

    def test_without_a_phone_it_asks_for_one_instead_of_showing_a_snippet(self):
        html = self.client.get(self.PAGE).content.decode()
        self.assertIn("Escribe el teléfono", html)
        self.assertNotIn("wa.me", html)

    def test_saving_produces_a_link_and_a_snippet(self):
        self.client.post(self.URL, {
            "widget_phone": "+57 300 123 4567", "widget_greeting": "Hola, info",
            "widget_label": "Escríbenos", "widget_position": "left",
        })
        row = MessagingSettings.load()
        self.assertEqual(row.widget_phone, "+573001234567")   # normalized
        self.assertIn("wa.me/573001234567", row.widget_url)
        self.assertIn("Hola%2C%20info", row.widget_url)        # greeting encoded
        html = self.client.get(self.PAGE).content.decode()
        self.assertIn("wa.me/573001234567", html)
        self.assertIn("widget-snippet", html)

    def test_a_malformed_phone_is_refused(self):
        html = self.client.post(self.URL, {"widget_phone": "+12"}).content.decode()
        self.assertIn("indicativo", html)
        self.assertEqual(MessagingSettings.load().widget_phone, "")

    def test_the_snippet_carries_no_javascript(self):
        # It gets pasted into sites this project does not control.
        self.client.post(self.URL, {"widget_phone": "+573001234567", "widget_position": "right"})
        html = self.client.get(self.PAGE).content.decode()
        snippet = html.split("widget-snippet", 1)[1].split("</pre>", 1)[0]
        self.assertNotIn("script", snippet.lower())


class SettingsSingletonTests(TestCase):
    def test_load_always_returns_the_same_row(self):
        a, b = MessagingSettings.load(), MessagingSettings.load()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(MessagingSettings.objects.count(), 1)

    def test_everything_starts_switched_off(self):
        row = MessagingSettings.load()
        self.assertFalse(row.welcome_enabled)
        self.assertFalse(row.assign_enabled)
        self.assertEqual(row.widget_phone, "")

    def test_widget_url_is_empty_without_a_phone(self):
        self.assertEqual(MessagingSettings.load().widget_url, "")
