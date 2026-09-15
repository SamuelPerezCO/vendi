"""The send path meets the price list.

``messaging.pricing`` could already quote a send and answer whether it would
cross the month's ceiling, but nothing called it: ``send_template`` wrote its
Message row with no billed amount, and the cost only ever appeared if Meta's
delivery receipt happened to arrive with a pricing object. So a send through a
provider that reports no billing, or one whose webhook was missed, was never
priced at all -- and the ceiling was a function with no callers.

These pin the wiring: every template send is priced when it is written, the
ceiling is enforced in the service rather than the dialog, and a send the
provider refused is billed nothing.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import Client, MessageTemplate
from messaging import services
from messaging.models import Conversation, Message


def template(category="marketing", **kwargs):
    kwargs.setdefault("name", "saludo")
    kwargs.setdefault("body", "Hola {{1}}")
    kwargs.setdefault("body_sample_values", ["Camila"])
    kwargs.setdefault("status", "aceptada")
    return MessageTemplate.objects.create(category=category, **kwargs)


def conversation(country="CO"):
    contact = Client.objects.create(
        first_name="Ana", last_name="Real", phone="+573001112233", country=country
    )
    return Conversation.objects.create(contact=contact, channel="whatsapp")


class PricedOnSendTests(TestCase):
    def test_a_send_freezes_what_it_cost_onto_the_row(self):
        chat, entry = conversation(), template()

        message = services.send_template(chat, entry, {"1": "Ana"})

        self.assertEqual(message.billed_category, "marketing")
        self.assertIsNotNone(message.billed_amount)
        self.assertGreater(message.billed_amount, Decimal("0"))
        self.assertEqual(message.billed_currency, "USD")

    def test_the_row_records_which_plantilla_it_came_from(self):
        chat, entry = conversation(), template()

        self.assertEqual(services.send_template(chat, entry, {"1": "Ana"}).template, entry)

    def test_the_amount_follows_the_category(self):
        chat = conversation()
        marketing = services.send_template(chat, template(name="m"), {"1": "Ana"})
        utility = services.send_template(
            chat, template(name="u", category="utility"), {"1": "Ana"}
        )

        # Colombia's utility rate is far below its marketing one.
        self.assertLess(utility.billed_amount, marketing.billed_amount)

    def test_a_refused_send_is_billed_nothing(self):
        chat, entry = conversation(), template()

        with patch(
            "messaging.testing.StubProvider.send_template",
            side_effect=RuntimeError("graph said no"),
        ):
            with self.assertRaises(services.SendFailed):
                services.send_template(chat, entry, {"1": "Ana"})

        message = Message.objects.get()
        self.assertEqual(message.status, "failed")
        self.assertEqual(message.billed_amount, Decimal("0"))


class MonthlyCeilingTests(TestCase):
    """The ceiling is enforced here, not in the dialog, so a bulk loop and a
    hand-crafted POST meet the same guard."""

    @override_settings(MESSAGING_MONTHLY_BUDGET="0.0001")
    def test_a_send_over_the_ceiling_is_refused_before_anything_is_written(self):
        chat, entry = conversation(), template()

        with self.assertRaises(services.BudgetExceeded):
            services.send_template(chat, entry, {"1": "Ana"})

        # Refused before the row, so the thread shows no trace of it.
        self.assertEqual(Message.objects.count(), 0)

    @override_settings(MESSAGING_MONTHLY_BUDGET="0.0001")
    def test_the_refusal_names_the_ceiling(self):
        chat, entry = conversation(), template()

        with self.assertRaises(services.BudgetExceeded) as caught:
            services.send_template(chat, entry, {"1": "Ana"})

        self.assertIn("presupuesto mensual", str(caught.exception))

    @override_settings(MESSAGING_MONTHLY_BUDGET="")
    def test_no_ceiling_configured_means_no_limit(self):
        chat, entry = conversation(), template()

        self.assertIsNotNone(services.send_template(chat, entry, {"1": "Ana"}).pk)


class SendableTemplatesTests(TestCase):
    def test_only_active_aceptadas_are_offered(self):
        # Meta refuses any other name as nonexistent (132001).
        template(name="ok")
        template(name="prueba_texto", status="pendiente")
        template(name="no", status="rechazada")
        template(name="off", is_active=False)

        self.assertEqual([t.name for t in services.sendable_templates()], ["ok"])

    def test_they_come_in_name_order(self):
        template(name="zz_ultima")
        template(name="aa_primera")

        self.assertEqual(
            [t.name for t in services.sendable_templates()], ["aa_primera", "zz_ultima"]
        )


class ConversationForClientTests(TestCase):
    """Writing to a client who has never written needs a thread to write into."""

    def test_a_client_with_no_thread_gets_one(self):
        client = Client.objects.create(first_name="Nueva", phone="+573009998877")

        chat = services.conversation_for_client(client)

        self.assertEqual(chat.contact, client)
        self.assertEqual(Conversation.objects.count(), 1)

    def test_an_existing_open_thread_is_reused(self):
        chat = conversation()

        self.assertEqual(services.conversation_for_client(chat.contact), chat)
        self.assertEqual(Conversation.objects.count(), 1)
