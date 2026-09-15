"""Tests for the messaging layer: webhook contract, 24h rule, Inbox wiring."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
from datetime import timedelta

import requests
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Client

from unittest.mock import MagicMock, Mock, patch

from . import services
from .models import Conversation, ConversationTag, Message, Tag
from .providers.meta import MetaProvider
from .providers.types import InboundEvent, MessageStatus, SendOutcomeUnknown
from .services import SendWindowClosed, send_message
from .testing import StubProvider

META_WEBHOOK_URL = "/webhooks/messaging/meta/"
META_APP_SECRET = "test-app-secret"
META_VERIFY_TOKEN = "test-verify-token"


def meta_signature(payload: str) -> dict:
    """The X-Hub-Signature-256 Meta would send for this exact body."""
    digest = hmac.new(
        META_APP_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def meta_payload(*, messages=None, statuses=None, contacts=None) -> str:
    """One webhook body in Meta's nested shape."""
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15551957906",
                     "phone_number_id": "1368700699649920"},
    }
    if contacts is not None:
        value["contacts"] = contacts
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {"id": "1783915395959608",
                 "changes": [{"field": "messages", "value": value}]}
            ],
        }
    )


def process(*events: dict) -> None:
    """Hand events to the service the webhook feeds once a signature checks
    out -- the half of the endpoint that decides what lands in the database."""
    services.process_inbound_events([InboundEvent(**event) for event in events])


def message_event(**overrides) -> dict:
    event = {
        "event_type": "message",
        "provider_message_id": "prov-msg-1",
        "from_number": "+573000000099",
        "body": "Hola, ¿tienen envíos a Cali?",
        "channel": "whatsapp",
        "contact_name": "Cliente Prueba",
    }
    event.update(overrides)
    return event


def outbound_event(**overrides) -> dict:
    event = {
        "event_type": "outbound_message",
        "provider_message_id": "prov-out-1",
        "to_number": "+573000000099",
        "body": "Claro, hacemos envíos a Cali.",
        "channel": "whatsapp",
    }
    event.update(overrides)
    return event


class InboundMessageTests(TestCase):
    """What a customer's message does to the database once the webhook has
    verified and parsed it (WebhookContractTests covers that first half)."""

    def test_inbound_message_creates_contact_conversation_and_message(self):
        process(message_event())

        contact = Client.objects.get(phone="+573000000099")
        self.assertEqual(contact.first_name, "Cliente Prueba")
        conversation = contact.conversations.get()
        self.assertEqual(conversation.unread_count, 1)
        self.assertIsNotNone(conversation.last_inbound_at)
        message = conversation.messages.get()
        self.assertEqual(message.direction, Message.INBOUND)
        self.assertEqual(message.provider_message_id, "prov-msg-1")

    def test_same_provider_message_id_twice_creates_one_message(self):
        """Providers retry deliveries; a retry must be a no-op."""
        process(message_event())
        process(message_event())

        self.assertEqual(Message.objects.count(), 1)
        # The duplicate must not bump counters either.
        self.assertEqual(Conversation.objects.get().unread_count, 1)

    def test_second_message_reuses_open_conversation(self):
        process(message_event())
        process(message_event(provider_message_id="prov-msg-2", body="¿Y a Medellín?"))
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Conversation.objects.get().unread_count, 2)

    def test_inbound_reopens_resolved_conversation_as_new_thread(self):
        process(message_event())
        conversation = Conversation.objects.get()
        conversation.status = Conversation.RESOLVED
        conversation.save()

        process(message_event(provider_message_id="prov-msg-2"))
        # Resolved threads are history: the new inbound starts a fresh one.
        self.assertEqual(Conversation.objects.count(), 2)


@override_settings(META_APP_SECRET=META_APP_SECRET)
class WebhookContractTests(TestCase):
    """The endpoint's contract, through Meta's webhook: verify the signature
    before trusting a byte of the body, then answer 200 no matter what."""

    def post(self, payload: str, headers=None):
        return self.client.post(
            META_WEBHOOK_URL, data=payload, content_type="application/json",
            headers=headers or {},
        )

    def inbound(self) -> str:
        return meta_payload(
            contacts=[{"wa_id": "573000000099", "profile": {"name": "Cliente Prueba"}}],
            messages=[{"id": "wamid.IN1", "from": "573000000099", "type": "text",
                       "text": {"body": "Hola, ¿tienen envíos a Cali?"}}],
        )

    def test_bad_signature_rejected_with_401_and_nothing_created(self):
        response = self.post(self.inbound(), {"X-Hub-Signature-256": "sha256=forged"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    def test_missing_signature_rejected_with_401(self):
        response = self.post(self.inbound())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Message.objects.count(), 0)

    def test_unparseable_payload_still_answers_200(self):
        """Non-200 would trigger a provider retry storm over the same junk."""
        payload = "this is not json"
        response = self.post(payload, meta_signature(payload))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.count(), 0)

    def test_unknown_provider_404s(self):
        response = self.client.post("/webhooks/messaging/nope/", data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 404)


class OutboundEventTests(TestCase):
    """A message *we* sent through a channel other than the Inbox itself --
    e.g. an agent replying straight from the paired WhatsApp phone. Contrast
    with InboundMessageTests: same processing, but this is our side of the
    thread, not the customer's, which is why unread/last_inbound_at/status
    don't move."""

    def test_outbound_event_creates_contact_conversation_and_message(self):
        process(outbound_event())

        contact = Client.objects.get(phone="+573000000099")
        message = contact.conversations.get().messages.get()
        self.assertEqual(message.direction, Message.OUTBOUND)
        self.assertEqual(message.body, "Claro, hacemos envíos a Cali.")
        self.assertEqual(message.provider_message_id, "prov-out-1")

    def test_outbound_event_does_not_bump_unread_or_set_last_inbound_at(self):
        process(outbound_event())
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.unread_count, 0)
        self.assertIsNone(conversation.last_inbound_at)

    def test_outbound_event_reuses_the_open_conversation_from_a_prior_inbound(self):
        process(message_event())
        process(outbound_event())

        self.assertEqual(Conversation.objects.count(), 1)
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.messages.count(), 2)
        # The customer's own message still counts as unread -- only the reply
        # (this event) shouldn't have touched it.
        self.assertEqual(conversation.unread_count, 1)

    def test_same_provider_message_id_twice_creates_one_outbound_message(self):
        process(outbound_event())
        process(outbound_event())
        self.assertEqual(Message.objects.count(), 1)

    def test_outbound_event_without_to_number_is_dropped_not_crashed(self):
        """process_inbound_events isolates bad events (see its docstring) --
        the webhook has already answered 200, so this must log, not raise."""
        process(outbound_event(to_number=""))
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    def test_self_chat_becomes_a_conversation_with_the_paired_number(self):
        """WhatsApp's "Yo" self-chat: to_number is the paired number's own,
        not a customer's -- still recorded, same as any other outbound event."""
        process(outbound_event(to_number="+573000000001", body="nota para mí"))
        contact = Client.objects.get(phone="+573000000001")
        self.assertEqual(contact.conversations.get().messages.get().body, "nota para mí")


class StatusEventTests(TestCase):
    def setUp(self):
        contact = Client.objects.create(first_name="Ana", phone="+573000000098")
        self.conversation = Conversation.objects.create(contact=contact)
        self.message = Message.objects.create(
            conversation=self.conversation,
            direction=Message.OUTBOUND,
            body="Hola",
            status=MessageStatus.SENT.value,
            provider_message_id="out-1",
        )

    def post_status(self, status: str):
        process({"event_type": "status", "provider_message_id": "out-1",
                 "status": MessageStatus(status)})

    def test_status_moves_forward(self):
        self.post_status("delivered")
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "delivered")

    def test_status_never_moves_backward(self):
        """Receipts arrive out of order; 'read' must not regress."""
        self.post_status("read")
        self.post_status("delivered")  # late retry of an older receipt
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "read")


class SendWindowTests(TestCase):
    def setUp(self):
        contact = Client.objects.create(first_name="Luis", phone="+573000000097")
        self.conversation = Conversation.objects.create(contact=contact)

    def test_send_outside_24h_window_raises_and_creates_nothing(self):
        self.conversation.last_inbound_at = timezone.now() - timedelta(hours=25)
        self.conversation.save()

        with self.assertRaises(SendWindowClosed):
            send_message(self.conversation, "Hola de nuevo")
        self.assertEqual(Message.objects.count(), 0)

    def test_send_with_no_inbound_ever_raises(self):
        """A thread the customer never wrote to has no window at all."""
        with self.assertRaises(SendWindowClosed):
            send_message(self.conversation, "Hola")

    def test_send_inside_window_succeeds(self):
        self.conversation.last_inbound_at = timezone.now() - timedelta(hours=1)
        self.conversation.save()

        message = send_message(self.conversation, "¡Claro que sí!")

        self.assertEqual(message.direction, Message.OUTBOUND)
        self.assertTrue(message.provider_message_id.startswith("stub-"))
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message_at, message.timestamp)


class UnconfirmedSendTests(TestCase):
    """A send that reached WhatsApp but never answered: the row stays queued,
    and the receipt that follows claims it by recipient. This is the bug
    where the CRM showed a red tick on a message the customer had received:
    the provider timed out, the row was stamped failed with no id, and every
    receipt for it was then dropped as unknown."""

    PHONE = "+573000000094"

    def setUp(self):
        self.contact = Client.objects.create(first_name="Hector", phone=self.PHONE)
        self.conversation = Conversation.objects.create(
            contact=self.contact, last_inbound_at=timezone.now()
        )

    def send_unconfirmed(self, body="Hola Hector") -> Message:
        with patch.object(StubProvider, "send_text", side_effect=SendOutcomeUnknown("slow")):
            with self.assertRaises(services.SendUnconfirmed):
                send_message(self.conversation, body)
        return self.conversation.messages.latest("pk")

    def receipt(self, status=MessageStatus.DELIVERED, to=PHONE, **extra) -> InboundEvent:
        return InboundEvent(
            event_type="status", provider_message_id="wamid.LATE",
            to_number=to, status=status, **extra,
        )

    def test_unconfirmed_send_stays_queued_with_no_id(self):
        message = self.send_unconfirmed()
        self.assertEqual(message.status, MessageStatus.QUEUED.value)
        self.assertIsNone(message.provider_message_id)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message_at, message.timestamp)

    def test_unconfirmed_is_still_a_send_failed_for_existing_handlers(self):
        self.assertTrue(issubclass(services.SendUnconfirmed, services.SendFailed))

    def test_other_errors_still_mark_the_row_failed(self):
        with patch.object(StubProvider, "send_text", side_effect=RuntimeError("400")):
            with self.assertRaises(services.SendFailed):
                send_message(self.conversation, "Hola")
        self.assertEqual(self.conversation.messages.get().status, "failed")

    def test_receipt_for_unknown_id_adopts_the_recent_queued_row(self):
        orphan = self.send_unconfirmed()

        services.process_inbound_events([self.receipt(pricing={"category": "service"})])

        orphan.refresh_from_db()
        self.assertEqual(orphan.provider_message_id, "wamid.LATE")
        self.assertEqual(orphan.status, "delivered")
        self.assertEqual(orphan.meta_pricing_category, "service")

    def test_receipt_matches_the_recipient_as_meta_spells_it(self):
        """Meta's recipient_id comes through _to_e164 with a '+', but an
        external writer may have stored the bare wa_id on the Client."""
        Client.objects.filter(pk=self.contact.pk).update(phone=self.PHONE.lstrip("+"))
        orphan = self.send_unconfirmed()

        services.process_inbound_events([self.receipt(status=MessageStatus.SENT)])

        orphan.refresh_from_db()
        self.assertEqual(orphan.provider_message_id, "wamid.LATE")
        self.assertEqual(orphan.status, "sent")

    def test_later_receipts_for_the_adopted_id_match_normally(self):
        orphan = self.send_unconfirmed()
        services.process_inbound_events([self.receipt(status=MessageStatus.SENT)])
        services.process_inbound_events([self.receipt(status=MessageStatus.READ)])
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, "read")
        self.assertEqual(self.conversation.messages.count(), 1)

    def test_the_newest_orphan_wins(self):
        older = self.send_unconfirmed("primero")
        newer = self.send_unconfirmed("segundo")

        services.process_inbound_events([self.receipt()])

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertIsNone(older.provider_message_id)
        self.assertEqual(newer.provider_message_id, "wamid.LATE")

    def test_a_failed_row_is_never_adopted(self):
        """'failed' is the platform's refusal; a receipt for the same customer
        minutes later belongs to some other message."""
        with patch.object(StubProvider, "send_text", side_effect=RuntimeError("400")):
            with self.assertRaises(services.SendFailed):
                send_message(self.conversation, "Hola")
        failed = self.conversation.messages.get()

        services.process_inbound_events([self.receipt()])

        failed.refresh_from_db()
        self.assertIsNone(failed.provider_message_id)
        self.assertEqual(failed.status, "failed")

    def test_an_old_orphan_is_not_adopted(self):
        orphan = self.send_unconfirmed()
        Message.objects.filter(pk=orphan.pk).update(
            timestamp=timezone.now() - services.UNCONFIRMED_SEND_WINDOW - timedelta(minutes=1)
        )

        services.process_inbound_events([self.receipt()])

        orphan.refresh_from_db()
        self.assertIsNone(orphan.provider_message_id)
        self.assertEqual(orphan.status, "queued")

    def test_the_window_is_measured_from_the_receipts_own_timestamp(self):
        """Meta retries webhooks for hours; the receipt carries the time the
        status happened, and that is what the send must be near."""
        orphan = self.send_unconfirmed()
        Message.objects.filter(pk=orphan.pk).update(
            timestamp=timezone.now() - timedelta(hours=3)
        )

        services.process_inbound_events([self.receipt(
            timestamp=timezone.now() - timedelta(hours=3) + timedelta(seconds=5)
        )])

        orphan.refresh_from_db()
        self.assertEqual(orphan.provider_message_id, "wamid.LATE")

    def test_another_customers_orphan_is_not_adopted(self):
        orphan = self.send_unconfirmed()
        Client.objects.create(first_name="Otra", phone="+573000000093")

        services.process_inbound_events([self.receipt(to="+573000000093")])

        orphan.refresh_from_db()
        self.assertIsNone(orphan.provider_message_id)

    def test_a_receipt_without_recipient_is_still_dropped(self):
        orphan = self.send_unconfirmed()
        services.process_inbound_events([self.receipt(to="")])
        orphan.refresh_from_db()
        self.assertIsNone(orphan.provider_message_id)


class InboxUITests(TestCase):
    """The messaging data showing up through the existing Inbox screen."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("asesor", password="x")
        self.contact = Client.objects.create(
            first_name="Camila", last_name="Pruebas", phone="+573000000095",
            channel="whatsapp",
        )
        now = timezone.now()
        self.conversation = Conversation.objects.create(
            contact=self.contact, channel="whatsapp", assigned_to=self.user,
            last_message_at=now, last_inbound_at=now, unread_count=3,
        )
        Message.objects.create(
            conversation=self.conversation, direction=Message.INBOUND,
            body="Hola, ¿tienen tallas grandes?", provider_message_id="ui-1",
            status="delivered", timestamp=now,
        )

    def test_conversation_list_shows_real_rows(self):
        response = self.client.get(reverse("section", args=["inbox"]))
        self.assertContains(response, "Camila Pruebas")
        self.assertContains(response, "asesor")        # line 2: assigned agent
        self.assertContains(response, "conv-row__dot")  # unread indicator
        self.assertNotContains(response, "Tu inbox está vacío")

    def test_channel_counts_reflect_unread_conversations(self):
        response = self.client.get(reverse("section", args=["inbox"]))
        counts = response.context["counts"]
        self.assertEqual(counts["whatsapp"], 1)
        self.assertEqual(counts["messenger"], 0)

    def test_tu_inbox_filter_needs_the_assigned_user(self):
        anonymous = self.client.get(reverse("inbox_list", args=["tu-inbox"]))
        self.assertContains(anonymous, "Tu inbox está vacío")

        self.client.force_login(self.user)
        assigned = self.client.get(reverse("inbox_list", args=["tu-inbox"]))
        self.assertContains(assigned, "Camila Pruebas")

    def test_sin_asignar_filter_shows_only_unassigned(self):
        response = self.client.get(reverse("inbox_list", args=["sin-asignar"]))
        self.assertContains(response, "Tu inbox está vacío")

        self.conversation.assigned_to = None
        self.conversation.save()
        response = self.client.get(reverse("inbox_list", args=["sin-asignar"]))
        self.assertContains(response, "Camila Pruebas")

    def test_opening_a_chat_renders_thread_and_clears_unread(self):
        response = self.client.get(reverse("inbox_chat", args=[self.conversation.pk]))
        self.assertContains(response, "tallas grandes")
        self.assertContains(response, "composer")          # window open -> live form
        self.assertContains(response, 'id="details-panel"')  # out-of-band column 5
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.unread_count, 0)

    def test_closed_window_disables_composer_with_notice(self):
        Conversation.objects.filter(pk=self.conversation.pk).update(
            last_inbound_at=timezone.now() - timedelta(hours=30)
        )
        response = self.client.get(reverse("inbox_chat", args=[self.conversation.pk]))
        self.assertContains(response, "ventana de 24 horas")
        self.assertNotContains(response, "composer__input")

    def test_sending_from_the_composer_creates_an_outbound_message(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("inbox_send", args=[self.conversation.pk]),
            {"body": "¡Sí! Hasta talla XXL."},
        )
        self.assertEqual(response.status_code, 200)
        outbound = self.conversation.messages.get(direction=Message.OUTBOUND)
        self.assertEqual(outbound.sent_by, self.user)
        self.assertTrue(outbound.provider_message_id.startswith("stub-"))

    def test_unconfirmed_send_shows_a_pending_notice_not_a_failure(self):
        self.client.force_login(self.user)
        with patch.object(StubProvider, "send_text", side_effect=SendOutcomeUnknown("slow")):
            response = self.client.post(
                reverse("inbox_send", args=[self.conversation.pk]), {"body": "Hola"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no lo reenvíes")
        self.assertContains(response, "msg-error--pending")
        self.assertNotContains(response, "Inténtalo de nuevo")
        outbound = self.conversation.messages.get(direction=Message.OUTBOUND)
        self.assertEqual(outbound.status, "queued")
        self.assertContains(response, "msg__status--queued")

    def test_failed_send_still_shows_the_retry_notice(self):
        self.client.force_login(self.user)
        with patch.object(StubProvider, "send_text", side_effect=RuntimeError("400")):
            response = self.client.post(
                reverse("inbox_send", args=[self.conversation.pk]), {"body": "Hola"}
            )
        self.assertContains(response, "Inténtalo de nuevo")
        self.assertNotContains(response, "msg-error--pending")
        self.assertContains(response, "msg__status--failed")


# --- Tags -------------------------------------------------------------------


def make_conversation(phone: str, channel: str = "whatsapp") -> Conversation:
    contact = Client.objects.create(first_name=f"C{phone[-2:]}", phone=phone)
    return Conversation.objects.create(
        contact=contact, channel=channel, last_message_at=timezone.now()
    )


class TagServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("agente", password="x")
        self.conversation = make_conversation("+573000000090")

    def test_tag_names_are_unique_case_insensitively(self):
        services.create_tag("Cliente Nuevo", "yellow", self.user)
        with self.assertRaises(services.TagNameTaken):
            services.create_tag("CLIENTE NUEVO", "green", self.user)
        self.assertEqual(Tag.objects.count(), 1)

    def test_create_tag_validates_name_and_color(self):
        with self.assertRaises(ValueError):
            services.create_tag("   ", "green")
        with self.assertRaises(ValueError):
            services.create_tag("Ventas", "#ff0000")  # raw hex is not a token

    def test_apply_tag_is_idempotent_and_audited(self):
        tag = services.create_tag("VIP", "purple", self.user)
        services.apply_tag([self.conversation], tag, self.user)
        services.apply_tag([self.conversation], tag, self.user)  # retry/no-op

        link = ConversationTag.objects.get()  # exactly one despite two calls
        self.assertEqual(link.tagged_by, self.user)
        self.assertIsNotNone(link.tagged_at)

    def test_archived_tag_cannot_be_applied_but_keeps_history(self):
        tag = services.create_tag("HISTÓRICA", "gray", self.user)
        services.apply_tag([self.conversation], tag, self.user)

        services.set_tag_archived(tag, True)

        # The 500-chats rule: archiving must not rewrite the past.
        self.assertEqual(ConversationTag.objects.count(), 1)
        with self.assertRaises(ValueError):
            services.apply_tag([make_conversation("+573000000091")], tag)

    def test_update_tag_renames_and_recolors(self):
        tag = services.create_tag("MAYORISTA", "blue", self.user)
        services.update_tag(tag, "MAYORISTA EFECTIVA", "green")
        tag.refresh_from_db()
        self.assertEqual((tag.name, tag.color), ("MAYORISTA EFECTIVA", "green"))

    def test_bulk_apply_and_remove(self):
        tag = services.create_tag("PROMO", "orange", self.user)
        conversations = [make_conversation(f"+57300000009{i}") for i in (2, 3, 4)]

        self.assertEqual(services.apply_tag(conversations, tag, self.user), 3)
        self.assertEqual(services.remove_tag(conversations[:2], tag), 2)
        self.assertEqual(ConversationTag.objects.count(), 1)


class TagFilterTests(TestCase):
    """AND semantics, composition with the nav filters, and flat query counts."""

    def setUp(self):
        self.tag_a = services.create_tag("A", "green")
        self.tag_b = services.create_tag("B", "blue")
        self.both = make_conversation("+573000000080")
        self.only_a = make_conversation("+573000000081", channel="messenger")
        self.untagged = make_conversation("+573000000082")
        services.apply_tag([self.both, self.only_a], self.tag_a)
        services.apply_tag([self.both], self.tag_b)

    def list_response(self, filter_key="todos", tags=()):
        return self.client.get(
            reverse("inbox_list", args=[filter_key]), {"tags": [t.pk for t in tags]}
        )

    def test_single_tag_narrows_the_list(self):
        html = self.list_response(tags=[self.tag_a]).content.decode()
        self.assertIn(self.both.contact.full_name, html)
        self.assertIn(self.only_a.contact.full_name, html)
        self.assertNotIn(self.untagged.contact.full_name, html)

    def test_multiple_tags_mean_and_not_or(self):
        html = self.list_response(tags=[self.tag_a, self.tag_b]).content.decode()
        self.assertIn(self.both.contact.full_name, html)
        self.assertNotIn(self.only_a.contact.full_name, html)  # has A but not B

    def test_tag_filter_composes_with_channel_filter(self):
        html = self.list_response("messenger", tags=[self.tag_a]).content.decode()
        self.assertIn(self.only_a.contact.full_name, html)
        self.assertNotIn(self.both.contact.full_name, html)  # tagged, wrong channel

    def test_query_count_stays_flat_as_the_list_grows(self):
        """Tags are prefetched, so more rows must not mean more queries."""
        url = reverse("inbox_list", args=["todos"])

        with CaptureQueriesContext(connection) as small:
            self.client.get(url)

        for i in range(10):  # 10 more conversations, all tagged
            conversation = make_conversation(f"+5730000001{i:02d}")
            services.apply_tag([conversation], self.tag_a)

        with CaptureQueriesContext(connection) as large:
            self.client.get(url)

        self.assertEqual(len(small), len(large))


class TagAdminUITests(TestCase):
    """The Etiquetas page (CRM > Gestión de clientes) and its endpoints."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="x")
        self.client.force_login(self.user)

    def test_panel_renders_tags_as_pills_with_usage(self):
        tag = services.create_tag("VENTA EFECTIVA", "green", self.user)
        services.apply_tag([make_conversation("+573000000070")], tag)

        response = self.client.get(
            reverse("section", args=["crm"]), {"view": "etiquetas"}
        )
        self.assertContains(response, "tag-pill--green")
        self.assertContains(response, "VENTA EFECTIVA")
        self.assertContains(response, "+ Crear etiqueta")
        self.assertEqual(response.context["tags"][0].usage, 1)

    def test_create_endpoint_creates_and_rerenders_table(self):
        response = self.client.post(
            reverse("tag_create"), {"name": "SHOPIFY NUEVO", "color": "purple"}
        )
        self.assertContains(response, "SHOPIFY NUEVO")
        tag = Tag.objects.get()
        self.assertEqual((tag.color, tag.created_by), ("purple", self.user))

    def test_duplicate_name_shows_error_not_a_second_tag(self):
        services.create_tag("VIP", "purple")
        response = self.client.post(
            reverse("tag_create"), {"name": "vip", "color": "green"}
        )
        self.assertContains(response, "Ya existe una etiqueta")
        self.assertEqual(Tag.objects.count(), 1)

    def test_edit_updates_every_pill_at_once(self):
        tag = services.create_tag("MAYORISTA", "blue")
        conversation = make_conversation("+573000000071")
        services.apply_tag([conversation], tag)

        self.client.post(
            reverse("tag_update", args=[tag.pk]),
            {"name": "MAYORISTA EFECTIVA", "color": "green"},
        )

        # The conversation list shows the new name/color without retagging --
        # the rows reference the Tag row, not a copied string.
        html = self.client.get(reverse("inbox_list", args=["todos"])).content.decode()
        self.assertIn("MAYORISTA EFECTIVA", html)
        self.assertIn("tag-pill--green", html)

    def test_archive_hides_from_pickers_but_not_from_rows(self):
        tag = services.create_tag("VIEJA", "gray")
        conversation = make_conversation("+573000000072")
        services.apply_tag([conversation], tag)

        self.client.post(reverse("tag_archive", args=[tag.pk]), {"archived": "1"})

        # Gone from the picker...
        picker = self.client.get(
            reverse("conversation_tags", args=[conversation.pk])
        ).content.decode()
        self.assertNotIn("VIEJA", picker)
        # ...but still on the tagged row, and the link rows survive.
        rows = self.client.get(reverse("inbox_list", args=["todos"])).content.decode()
        self.assertIn("VIEJA", rows)
        self.assertEqual(ConversationTag.objects.count(), 1)


class TagPickerTests(TestCase):
    """The per-conversation picker shared by the rows and the chat header."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("agente", password="x")
        self.client.force_login(self.user)
        self.conversation = make_conversation("+573000000060")
        self.tag = services.create_tag("PROMO", "orange")

    def picker_url(self):
        return reverse("conversation_tags", args=[self.conversation.pk])

    def test_get_lists_tags_and_marks_applied_ones(self):
        services.apply_tag([self.conversation], self.tag)
        response = self.client.get(self.picker_url())
        self.assertContains(response, "PROMO")
        self.assertContains(response, "checked")

    def test_toggle_add_then_remove(self):
        self.client.post(self.picker_url(), {"tag_id": self.tag.pk, "action": "add"})
        self.assertEqual(self.conversation.tags.count(), 1)
        self.assertEqual(ConversationTag.objects.get().tagged_by, self.user)

        self.client.post(self.picker_url(), {"tag_id": self.tag.pk, "action": "remove"})
        self.assertEqual(self.conversation.tags.count(), 0)

    def test_toggle_response_updates_pills_out_of_band(self):
        response = self.client.post(
            self.picker_url(), {"tag_id": self.tag.pk, "action": "add"}
        )
        # All three pill surfaces: list row, chat header, details panel.
        self.assertContains(response, f'id="conv-tags-{self.conversation.pk}"')
        self.assertContains(response, f'id="chat-tags-{self.conversation.pk}"')
        self.assertContains(response, f'id="details-tags-{self.conversation.pk}"')
        self.assertContains(response, "hx-swap-oob")

    def test_details_panel_has_the_tag_editor(self):
        services.apply_tag([self.conversation], self.tag)
        response = self.client.get(reverse("inbox_chat", args=[self.conversation.pk]))
        self.assertContains(response, "+ Añadir etiqueta")
        self.assertContains(response, f'id="details-tags-{self.conversation.pk}"')
        self.assertContains(response, "tag-pill__remove")  # the × on each pill

    def test_conversation_list_has_no_picker(self):
        """Tags in the list are display-only; editing lives in column 5."""
        html = self.client.get(reverse("inbox_list", args=["todos"])).content.decode()
        self.assertNotIn("tag-picker", html)
        self.assertNotIn("Añadir etiqueta", html)

    def test_inline_create_invents_a_tag_without_leaving_the_inbox(self):
        response = self.client.post(self.picker_url(), {"new_name": "URGENTE"})
        self.assertEqual(response.status_code, 200)
        tag = Tag.objects.get(name="URGENTE")
        self.assertIn(tag, self.conversation.tags.all())
        self.assertEqual(tag.created_by, self.user)

    def test_search_offers_create_only_when_nothing_matches(self):
        no_match = self.client.get(self.picker_url(), {"q": "MAYORISTA"})
        self.assertContains(no_match, "Crear «MAYORISTA»")
        match = self.client.get(self.picker_url(), {"q": "PROMO"})
        self.assertNotContains(match, "Crear «")


class BulkTagTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("agente", password="x")
        self.client.force_login(self.user)
        self.tag = services.create_tag("PROMO", "orange")
        self.conversations = [make_conversation(f"+5730000000{i}") for i in (50, 51, 52)]

    def test_bulk_add_tags_every_selected_conversation(self):
        response = self.client.post(
            reverse("inbox_tags_bulk"),
            {
                "tag_id": self.tag.pk,
                "action": "add",
                "selected": [c.pk for c in self.conversations[:2]],
                "filter": "todos",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConversationTag.objects.count(), 2)
        # The response is the refreshed list, pills included.
        self.assertContains(response, "PROMO")

    def test_bulk_remove(self):
        services.apply_tag(self.conversations, self.tag)
        self.client.post(
            reverse("inbox_tags_bulk"),
            {
                "tag_id": self.tag.pk,
                "action": "remove",
                "selected": [c.pk for c in self.conversations],
                "filter": "todos",
            },
        )
        self.assertEqual(ConversationTag.objects.count(), 0)


@override_settings(
    META_APP_SECRET=META_APP_SECRET,
    META_VERIFY_TOKEN=META_VERIFY_TOKEN,
    META_ACCESS_TOKEN="test-token",
    META_PHONE_NUMBER_ID="1368700699649920",
)
class MetaProviderTests(TestCase):
    """The Meta Cloud API provider: HMAC webhooks, the subscribe handshake,
    nested payload parsing and Graph sends."""

    def setUp(self):
        self.provider = MetaProvider()

    def request_for(self, payload: str, headers=None):
        return self.client.post(
            META_WEBHOOK_URL,
            data=payload,
            content_type="application/json",
            headers=headers or {},
        ).wsgi_request

    # --- verify_signature -------------------------------------------------

    def test_verify_signature_accepts_meta_hmac(self):
        payload = meta_payload(messages=[])
        request = self.request_for(payload, meta_signature(payload))
        self.assertTrue(self.provider.verify_signature(request))

    def test_verify_signature_rejects_forged_digest(self):
        payload = meta_payload(messages=[])
        request = self.request_for(payload, {"X-Hub-Signature-256": "sha256=deadbeef"})
        self.assertFalse(self.provider.verify_signature(request))

    def test_verify_signature_rejects_signature_of_a_different_body(self):
        """The HMAC must cover the body actually received, not any body."""
        request = self.request_for(
            meta_payload(messages=[{"id": "wamid.X", "from": "573000000099",
                                    "type": "text", "text": {"body": "tampered"}}]),
            meta_signature(meta_payload(messages=[])),
        )
        self.assertFalse(self.provider.verify_signature(request))

    def test_verify_signature_rejects_missing_header(self):
        request = self.request_for(meta_payload(messages=[]))
        self.assertFalse(self.provider.verify_signature(request))

    @override_settings(META_APP_SECRET="")
    def test_verify_signature_fails_closed_without_app_secret(self):
        """An unconfigured secret must reject traffic, not wave it through."""
        payload = meta_payload(messages=[])
        digest = hmac.new(b"", payload.encode("utf-8"), hashlib.sha256).hexdigest()
        request = self.request_for(payload, {"X-Hub-Signature-256": f"sha256={digest}"})
        self.assertFalse(self.provider.verify_signature(request))

    # --- handshake --------------------------------------------------------

    def test_handshake_echoes_challenge_for_matching_token(self):
        response = self.client.get(
            META_WEBHOOK_URL,
            {"hub.mode": "subscribe", "hub.verify_token": META_VERIFY_TOKEN,
             "hub.challenge": "1158201444"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "1158201444")

    def test_handshake_rejects_wrong_token(self):
        response = self.client.get(
            META_WEBHOOK_URL,
            {"hub.mode": "subscribe", "hub.verify_token": "guessed",
             "hub.challenge": "1158201444"},
        )
        self.assertEqual(response.status_code, 403)

    def test_handshake_rejects_wrong_mode(self):
        response = self.client.get(
            META_WEBHOOK_URL,
            {"hub.mode": "unsubscribe", "hub.verify_token": META_VERIFY_TOKEN,
             "hub.challenge": "1158201444"},
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(META_VERIFY_TOKEN="")
    def test_handshake_fails_closed_without_verify_token(self):
        response = self.client.get(
            META_WEBHOOK_URL,
            {"hub.mode": "subscribe", "hub.verify_token": "",
             "hub.challenge": "1158201444"},
        )
        self.assertEqual(response.status_code, 403)

    # --- parse_webhook ----------------------------------------------------

    def test_parse_webhook_inbound_text(self):
        payload = meta_payload(
            contacts=[{"wa_id": "573000000099", "profile": {"name": "Cliente Real"}}],
            messages=[{
                "id": "wamid.HBgMNTczMDAwMDAwMDk5",
                "from": "573000000099",
                "timestamp": "1767225600",
                "type": "text",
                "text": {"body": "Hola, ¿tienen envíos a Cali?"},
            }],
        )
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, InboundEvent)
        self.assertEqual(event.event_type, "message")
        self.assertEqual(event.provider_message_id, "wamid.HBgMNTczMDAwMDAwMDk5")
        # Meta sends bare digits; the CRM stores E.164.
        self.assertEqual(event.from_number, "+573000000099")
        self.assertEqual(event.to_number, "+15551957906")
        self.assertEqual(event.body, "Hola, ¿tienen envíos a Cali?")
        self.assertEqual(event.contact_name, "Cliente Real")
        self.assertEqual(event.channel, "whatsapp")
        # unix seconds -> aware UTC datetime
        self.assertEqual(
            event.timestamp.isoformat(), "2026-01-01T00:00:00+00:00"
        )

    def test_parse_webhook_status_receipt(self):
        payload = meta_payload(statuses=[{
            "id": "wamid.SENT1",
            "status": "delivered",
            "timestamp": "1767225600",
            "recipient_id": "573000000099",
        }])
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "status")
        self.assertEqual(events[0].status, MessageStatus.DELIVERED)
        self.assertEqual(events[0].provider_message_id, "wamid.SENT1")

    def test_parse_webhook_flattens_a_batch(self):
        """One POST can carry several messages and receipts at once."""
        payload = meta_payload(
            contacts=[{"wa_id": "573000000099", "profile": {"name": "Ana"}}],
            messages=[
                {"id": "wamid.A", "from": "573000000099", "timestamp": "1767225600",
                 "type": "text", "text": {"body": "primera"}},
                {"id": "wamid.B", "from": "573000000099", "timestamp": "1767225601",
                 "type": "text", "text": {"body": "segunda"}},
            ],
            statuses=[{"id": "wamid.SENT1", "status": "read",
                       "recipient_id": "573000000099"}],
        )
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(len(events), 3)
        self.assertEqual([e.event_type for e in events],
                         ["message", "message", "status"])
        self.assertEqual(events[2].status, MessageStatus.READ)

    def test_parse_webhook_skips_unknown_status_but_keeps_the_batch(self):
        payload = meta_payload(
            messages=[{"id": "wamid.A", "from": "573000000099", "type": "text",
                       "text": {"body": "hola"}}],
            statuses=[{"id": "wamid.SENT1", "status": "warp-speed"}],
        )
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "message")

    def test_parse_webhook_interactive_reply_becomes_its_title(self):
        payload = meta_payload(messages=[{
            "id": "wamid.I", "from": "573000000099", "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "yes", "title": "Sí, confirmo"}},
        }])
        events = self.provider.parse_webhook(self.request_for(payload))
        self.assertEqual(events[0].body, "Sí, confirmo")

    @staticmethod
    def mock_media_responses(mock_get, *, mime_type: str, data: bytes):
        """Wire ``requests.get`` for the two calls behind one media message:
        the id lookup (JSON with the CDN url) and the streamed download."""
        lookup = Mock()
        lookup.raise_for_status.return_value = None
        lookup.json.return_value = {
            "url": "https://cdn.example/media",
            "mime_type": mime_type,
            "file_size": len(data),
        }
        download = MagicMock()  # Magic: the code uses it as a context manager
        download.raise_for_status.return_value = None
        download.iter_content.return_value = [data]
        mock_get.side_effect = [lookup, download]

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="mvp-crm-test-media-"))
    @patch("messaging.providers.meta.requests.get")
    def test_parse_webhook_image_is_downloaded_into_local_storage(self, mock_get):
        """Meta's CDN link is token-gated and expires in minutes, so the
        webhook must store the bytes and hand the app a durable local URL."""
        self.mock_media_responses(mock_get, mime_type="image/jpeg", data=b"jpegbytes")

        payload = meta_payload(messages=[{
            "id": "wamid.IMG", "from": "573000000099", "type": "image",
            "image": {"id": "media-123", "caption": "mira esto"},
        }])
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(events[0].body, "mira esto")
        self.assertEqual(events[0].media_type, "image")
        self.assertEqual(events[0].media_url, "/media/whatsapp/media-123.jpg")
        from django.core.files.storage import default_storage
        with default_storage.open("whatsapp/media-123.jpg") as stored:
            self.assertEqual(stored.read(), b"jpegbytes")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="mvp-crm-test-media-"))
    @patch("messaging.providers.meta.requests.get")
    def test_parse_webhook_sticker_is_stored_as_webp(self, mock_get):
        self.mock_media_responses(mock_get, mime_type="image/webp", data=b"webpbytes")

        payload = meta_payload(messages=[{
            "id": "wamid.STK", "from": "573000000099", "type": "sticker",
            "sticker": {"id": "sticker-9"},
        }])
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(events[0].body, "[sticker]")
        self.assertEqual(events[0].media_type, "sticker")
        self.assertEqual(events[0].media_url, "/media/whatsapp/sticker-9.webp")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="mvp-crm-test-media-"))
    @patch("messaging.providers.meta.requests.get")
    def test_media_download_is_idempotent_across_webhook_retries(self, mock_get):
        """A retried webhook must reuse the stored file, not download again."""
        self.mock_media_responses(mock_get, mime_type="image/webp", data=b"webpbytes")
        payload = meta_payload(messages=[{
            "id": "wamid.STK", "from": "573000000099", "type": "sticker",
            "sticker": {"id": "sticker-9"},
        }])
        first = self.provider.parse_webhook(self.request_for(payload))

        # The retry: only the id lookup fires, the stored file answers.
        lookup = Mock()
        lookup.raise_for_status.return_value = None
        lookup.json.return_value = {
            "url": "https://cdn.example/media", "mime_type": "image/webp",
        }
        mock_get.side_effect = [lookup]
        second = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(first[0].media_url, second[0].media_url)

    @patch("messaging.providers.meta.requests.get")
    def test_media_resolution_failure_does_not_break_the_webhook(self, mock_get):
        """An image lookup that fails must still land the message."""
        mock_get.side_effect = requests.RequestException("graph down")

        payload = meta_payload(messages=[{
            "id": "wamid.IMG", "from": "573000000099", "type": "image",
            "image": {"id": "media-123"},
        }])
        events = self.provider.parse_webhook(self.request_for(payload))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].media_url, "")
        self.assertEqual(events[0].media_type, "")  # no media to render
        self.assertEqual(events[0].body, "[imagen]")  # placeholder, not an empty bubble

    def test_parse_webhook_rejects_unparseable_payload(self):
        with self.assertRaises(ValueError):
            self.provider.parse_webhook(self.request_for("not json"))

    def test_parse_webhook_rejects_payload_without_entry(self):
        with self.assertRaises(ValueError):
            self.provider.parse_webhook(self.request_for(json.dumps({"object": "x"})))

    # --- endpoint (signature + parse + process) ---------------------------

    def test_end_to_end_via_endpoint(self):
        payload = meta_payload(
            contacts=[{"wa_id": "573000000097", "profile": {"name": "Nuevo Cliente"}}],
            messages=[{
                "id": "wamid.E2E", "from": "573000000097", "timestamp": "1767225600",
                "type": "text", "text": {"body": "Buenas, ¿tienen stock?"},
            }],
        )
        response = self.client.post(
            META_WEBHOOK_URL, data=payload, content_type="application/json",
            headers=meta_signature(payload),
        )

        self.assertEqual(response.status_code, 200)
        contact = Client.objects.get(phone="+573000000097")
        self.assertEqual(contact.first_name, "Nuevo Cliente")
        self.assertEqual(
            contact.conversations.get().messages.get().body, "Buenas, ¿tienen stock?"
        )

    def test_endpoint_rejects_bad_signature(self):
        payload = meta_payload(messages=[])
        response = self.client.post(
            META_WEBHOOK_URL, data=payload, content_type="application/json",
            headers={"X-Hub-Signature-256": "sha256=forged"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Message.objects.count(), 0)

    def test_endpoint_is_idempotent_on_retries(self):
        """Meta re-delivers aggressively; the same wamid must not double up."""
        payload = meta_payload(messages=[{
            "id": "wamid.RETRY", "from": "573000000096", "timestamp": "1767225600",
            "type": "text", "text": {"body": "una sola vez"},
        }])
        for _ in range(2):
            response = self.client.post(
                META_WEBHOOK_URL, data=payload, content_type="application/json",
                headers=meta_signature(payload),
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.filter(provider_message_id="wamid.RETRY").count(), 1)

    # --- sending (Graph call mocked) --------------------------------------

    @patch("messaging.providers.meta.requests.post")
    def test_send_text_posts_to_graph_and_returns_wamid(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"messages": [{"id": "wamid.OUT1"}]}

        message_id = self.provider.send_text("+573000000099", "Hola desde el CRM")

        self.assertEqual(message_id, "wamid.OUT1")
        url = mock_post.call_args.args[0]
        self.assertIn("1368700699649920/messages", url)
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["messaging_product"], "whatsapp")
        self.assertEqual(sent["to"], "+573000000099")
        self.assertEqual(sent["text"]["body"], "Hola desde el CRM")
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-token")

    @patch("messaging.providers.meta.requests.post")
    def test_send_read_timeout_is_unconfirmed_not_failed(self, mock_post):
        """Graph took the request and never answered: the message may be on
        its way, so this is SendOutcomeUnknown, not a plain error."""
        mock_post.side_effect = requests.ReadTimeout("graph is slow today")

        with self.assertRaises(SendOutcomeUnknown):
            self.provider.send_text("+573000000099", "Hola")
        self.assertEqual(mock_post.call_args.kwargs["timeout"], (5, 20))

    @patch("messaging.providers.meta.requests.post")
    def test_send_connect_timeout_is_a_plain_failure(self, mock_post):
        """Never reached Graph: nothing was sent, the ordinary failure path."""
        mock_post.side_effect = requests.ConnectTimeout("no route")

        with self.assertRaises(requests.ConnectTimeout):
            self.provider.send_text("+573000000099", "Hola")

    @patch("messaging.providers.meta.requests.post")
    def test_send_template_builds_positional_parameters_in_order(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"messages": [{"id": "wamid.T1"}]}

        self.provider.send_template("+573000000099", "order_confirmation",
                                    {"2": "mañana", "1": "Ana"})

        template = mock_post.call_args.kwargs["json"]["template"]
        self.assertEqual(template["name"], "order_confirmation")
        self.assertEqual(template["language"]["code"], "es")
        self.assertEqual(
            [p["text"] for p in template["components"][0]["parameters"]],
            ["Ana", "mañana"],
        )

    @patch("messaging.providers.meta.requests.post")
    def test_send_template_builds_named_parameters(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"messages": [{"id": "wamid.T2"}]}

        self.provider.send_template("+573000000099", "recordatorio",
                                    {"nombre": "Ana", "_language": "en_US"})

        template = mock_post.call_args.kwargs["json"]["template"]
        self.assertEqual(template["language"]["code"], "en_US")
        parameter = template["components"][0]["parameters"][0]
        self.assertEqual(parameter["parameter_name"], "nombre")
        self.assertEqual(parameter["text"], "Ana")

    @patch("messaging.providers.meta.requests.post")
    def test_send_template_does_not_mutate_the_caller_s_params(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"messages": [{"id": "wamid.T3"}]}

        params = {"nombre": "Ana", "_language": "en_US"}
        self.provider.send_template("+573000000099", "recordatorio", params)

        self.assertEqual(params, {"nombre": "Ana", "_language": "en_US"})

    @override_settings(META_ACCESS_TOKEN="", META_PHONE_NUMBER_ID="")
    def test_send_without_credentials_raises_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            self.provider.send_text("+573000000099", "hola")


class InlineMediaTests(TestCase):
    """media_type flows event -> Message row and drives inline rendering."""

    def process(self, **overrides) -> Message:
        event = InboundEvent(
            event_type="message",
            provider_message_id="prov-media-1",
            from_number="+573000000099",
            **overrides,
        )
        services.process_inbound_events([event])
        return Message.objects.get(provider_message_id="prov-media-1")

    def test_media_type_lands_on_the_message_row(self):
        message = self.process(
            body="[sticker]", media_url="/media/whatsapp/s.webp", media_type="sticker"
        )
        self.assertEqual(message.media_type, "sticker")
        self.assertTrue(message.is_inline_image)

    def test_placeholder_body_hides_behind_the_inline_image(self):
        message = self.process(
            body="[sticker]", media_url="/media/whatsapp/s.webp", media_type="sticker"
        )
        self.assertEqual(message.display_body, "")

    def test_caption_still_shows_under_the_inline_image(self):
        message = self.process(
            body="mira esto", media_url="/media/whatsapp/i.jpg", media_type="image"
        )
        self.assertEqual(message.display_body, "mira esto")

    def test_documents_keep_the_download_link_and_their_placeholder(self):
        message = self.process(
            body="[documento]", media_url="/media/whatsapp/d.pdf", media_type="document"
        )
        self.assertFalse(message.is_inline_image)
        self.assertEqual(message.display_body, "[documento]")

    def test_rows_from_before_the_field_existed_stay_download_links(self):
        message = self.process(body="[imagen]", media_url="/media/old.jpg")
        self.assertFalse(message.is_inline_image)
        self.assertEqual(message.display_body, "[imagen]")

    def test_thread_renders_the_inline_image_tag(self):
        """End to end through the Inbox view: the bubble carries an <img>."""
        message = self.process(
            body="[sticker]", media_url="/media/whatsapp/s.webp", media_type="sticker"
        )
        user = get_user_model().objects.create_user("agente", password="x")
        self.client.force_login(user)
        response = self.client.get(
            reverse("inbox_thread", args=[message.conversation_id])
        )
        self.assertContains(response, '<img src="/media/whatsapp/s.webp"')
        self.assertNotContains(response, "Archivo adjunto")
        # The placeholder survives only as the image's alt text, not as a
        # visible line under the sticker.
        self.assertContains(response, "[sticker]", count=1)
        self.assertContains(response, 'alt="[sticker]"')
