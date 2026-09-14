"""Tests for starting a conversation from our side: the Nuevo chat modal,
the plantilla send behind it (messaging.services.send_template) and the
closed composer's picker."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Client, MessageTemplate
from messaging import services as messaging_services
from messaging.models import Conversation, Message
from messaging.providers.fake import FakeProvider
from messaging.providers.types import InboundEvent


def template(name="saludo_inicial", body="Hola {{1}}, ¿en qué te ayudo?",
             samples=("Camila",), status="aceptada", is_active=True):
    return MessageTemplate.objects.create(
        name=name, body=body, body_sample_values=list(samples),
        status=status, is_active=is_active, language="es",
    )


def client(first_name="Camila", phone="+573000000777"):
    return Client.objects.create(first_name=first_name, phone=phone, channel="whatsapp")


class SendTemplateServiceTests(TestCase):
    def test_sends_outside_the_window_and_records_the_rendered_body(self):
        contact = client()
        conversation = Conversation.objects.create(contact=contact, channel="whatsapp")
        self.assertFalse(conversation.is_within_24h_window)   # never wrote in
        tpl = template()
        with mock.patch.object(FakeProvider, "send_template", return_value="tpl-1") as send:
            message = messaging_services.send_template(conversation, tpl, {"1": "Pedro"})
        send.assert_called_once_with(
            to="+573000000777",
            template_name="saludo_inicial",
            # The AGENT's value, not the editor's sample -- the whole point.
            params={
                "1": "Pedro",
                "_language": "es",
                "_rendered": "Hola Pedro, ¿en qué te ayudo?",
            },
        )
        self.assertEqual(message.body, "Hola Pedro, ¿en qué te ayudo?")
        self.assertEqual(message.provider_message_id, "tpl-1")
        self.assertTrue(message.is_outbound)
        conversation.refresh_from_db()
        self.assertEqual(conversation.last_message_at, message.timestamp)
        # Our send does not open the window -- only the customer's reply can.
        self.assertIsNone(conversation.last_inbound_at)

    def test_a_provider_error_keeps_the_row_as_failed(self):
        conversation = Conversation.objects.create(contact=client(), channel="whatsapp")
        with mock.patch.object(FakeProvider, "send_template", side_effect=RuntimeError("no")):
            with self.assertRaises(messaging_services.SendFailed):
                messaging_services.send_template(conversation, template(), {"1": "Pedro"})
        self.assertEqual(Message.objects.get().status, "failed")

    def test_start_conversation_reuses_the_open_thread(self):
        contact = client()
        first = messaging_services.start_conversation(contact)
        self.assertEqual(messaging_services.start_conversation(contact), first)
        first.status = Conversation.RESOLVED
        first.save()
        self.assertNotEqual(messaging_services.start_conversation(contact), first)


class NewChatModalTests(TestCase):
    URL = reverse("inbox_new_chat")

    def test_the_nav_button_opens_the_modal_from_this_route(self):
        html = self.client.get(reverse("section", args=["inbox"])).content.decode()
        self.assertIn('data-dialog-open="newchat-modal"', html)
        self.assertIn(f'hx-get="{self.URL}"', html)
        self.assertIn('id="newchat-modal-body"', html)

    def test_get_lists_clients_and_plantillas(self):
        camila = client()
        template(name="saludo_inicial")
        template(name="rechazada", status="rechazada")
        template(name="apagada", is_active=False)
        html = self.client.get(self.URL).content.decode()
        self.assertIn("Camila · +573000000777", html)
        self.assertIn("saludo_inicial", html)
        self.assertIn("Hola Camila", html)             # rendered with samples
        self.assertNotIn("rechazada", html)
        self.assertNotIn("apagada", html)
        self.assertIn(f'value="{camila.pk}"', html)

    def test_first_open_renders_the_checked_plantillas_variables(self):
        """The picker checks the first plantilla by default, so its {{n}}
        inputs must be there from the start (prefilled with the samples) --
        with a single plantilla there is no change event to fetch them."""
        client()
        tpl = template()
        html = self.client.get(self.URL).content.decode()
        self.assertIn(f'name="var_{tpl.pk}_1"', html)
        self.assertIn('value="Camila"', html)
        self.assertIn("Datos de la plantilla", html)

    def test_cliente_param_preselects(self):
        camila = client()
        client("Bruno", "+525512345678")
        html = self.client.get(self.URL, {"cliente": camila.pk}).content.decode()
        picked = html.split(f'value="{camila.pk}"', 1)[1].split(">", 1)[0]
        self.assertIn("selected", picked)

    def test_without_plantillas_the_form_explains_and_disables_send(self):
        client()
        html = self.client.get(self.URL).content.decode()
        self.assertIn("No hay plantillas activas", html)
        self.assertIn("view=plantillas-whatsapp", html)
        self.assertIn("disabled", html)

    def test_post_sends_the_plantilla_and_opens_the_new_thread(self):
        camila = client()
        tpl = template()
        response = self.client.post(
            self.URL, {"cliente": camila.pk, "plantilla": tpl.pk, f"var_{tpl.pk}_1": "Camila"}
        )
        html = response.content.decode()
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.contact, camila)
        self.assertEqual(conversation.channel, "whatsapp")
        self.assertEqual(Message.objects.get().body, "Hola Camila, ¿en qué te ayudo?")
        # The answer lands in the modal body: the dismiss marker directly,
        # everything behind it out-of-band.
        self.assertIn("data-dialog-dismiss", html)
        self.assertIn(f'data-conversation-id="{conversation.pk}"', html)
        for oob in ("chat-panel", "details-panel", "conv-list"):
            with self.subTest(oob):
                self.assertIn(f'id="{oob}" hx-swap-oob="innerHTML"', html)
        # A brand-new thread has no window yet, so what opens is the closed
        # composer with the picker, not the free-text box.
        self.assertIn("Enviar plantilla", html)

    def test_post_into_an_existing_open_thread_reuses_it(self):
        camila = client()
        existing = Conversation.objects.create(contact=camila, channel="whatsapp")
        tpl = template()
        self.client.post(
            self.URL, {"cliente": camila.pk, "plantilla": tpl.pk, f"var_{tpl.pk}_1": "Camila"}
        )
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(existing.messages.count(), 1)

    def test_both_outcomes_target_the_modal_body(self):
        """A rejected submit used to swap the form into #chat-panel, wiping
        the chat column; both branches answer into the modal now."""
        client()
        template()
        html = self.client.get(self.URL).content.decode()
        self.assertIn('hx-target="#newchat-modal-body"', html)
        self.assertNotIn('hx-target="#chat-panel"', html)

    def test_missing_client_or_plantilla_re_renders_with_a_message(self):
        camila = client()
        tpl = template()
        html = self.client.post(self.URL, {"plantilla": tpl.pk}).content.decode()
        self.assertIn("Elige a quién escribirle", html)
        html = self.client.post(self.URL, {"cliente": camila.pk}).content.decode()
        self.assertIn("Elige una plantilla", html)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_rejected_plantilla_cannot_be_sent(self):
        camila = client()
        bad = template(name="mala", status="rechazada")
        html = self.client.post(self.URL, {"cliente": camila.pk, "plantilla": bad.pk}).content.decode()
        self.assertIn("Elige una plantilla", html)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_provider_failure_keeps_the_modal_open(self):
        camila = client()
        tpl = template()
        with mock.patch.object(FakeProvider, "send_template", side_effect=RuntimeError("no")):
            html = self.client.post(
                self.URL,
                {"cliente": camila.pk, "plantilla": tpl.pk, f"var_{tpl.pk}_1": "Camila"},
            ).content.decode()
        self.assertIn("No se pudo enviar la plantilla", html)
        self.assertNotIn("data-dialog-dismiss", html)

    def test_nuevo_query_param_autoloads_the_modal_on_that_client(self):
        camila = client()
        html = self.client.get(
            reverse("section", args=["inbox"]), {"nuevo": camila.pk}
        ).content.decode()
        self.assertIn("data-dialog-autoshow", html)
        self.assertIn(f'hx-get="{self.URL}?cliente={camila.pk}"', html)
        self.assertIn('hx-trigger="load"', html)

    def test_the_crm_client_card_links_here(self):
        camila = client()
        html = self.client.get(reverse("cliente_detail", args=[camila.pk])).content.decode()
        self.assertIn(f"?nuevo={camila.pk}", html)
        self.assertIn("Nuevo chat", html)


class ClosedComposerTests(TestCase):
    """A closed 24h window offers the Enviar plantilla dialog -- the one that
    fills the plantilla's {{n}} for this customer. (The dialog's own
    behaviour is covered in messaging/core tests for inbox_template_send.)"""

    def setUp(self):
        self.conversation = Conversation.objects.create(
            contact=client(), channel="whatsapp",
            last_inbound_at=timezone.now() - timedelta(hours=30),
        )

    def test_a_closed_window_offers_the_plantilla_dialog_not_free_text(self):
        template()
        html = self.client.get(
            reverse("inbox_chat", args=[self.conversation.pk])
        ).content.decode()
        self.assertIn("ventana de 24 horas", html)
        self.assertIn(
            reverse("inbox_template_send", args=[self.conversation.pk]), html
        )
        self.assertIn("Enviar plantilla", html)
        self.assertNotIn('name="body"', html)   # no free text offered

    def test_an_open_window_offers_both_the_composer_and_the_dialog(self):
        self.conversation.last_inbound_at = timezone.now()
        self.conversation.save()
        template()
        html = self.client.get(
            reverse("inbox_chat", args=[self.conversation.pk])
        ).content.decode()
        self.assertIn('name="body"', html)
        # The plantilla path never depends on which state the thread is in.
        self.assertIn("Enviar plantilla", html)


class ComposerFollowsTheWindowTests(TestCase):
    """The open thread's 5-second poll unlocks the composer the moment the
    customer replies, and locks it when 24 hours run out -- nobody has to
    reopen the chat. It never re-renders the composer otherwise: a
    half-typed draft must survive every ordinary poll."""

    def setUp(self):
        self.contact = client()
        self.conversation = Conversation.objects.create(
            contact=self.contact, channel="whatsapp",
            last_inbound_at=timezone.now() - timedelta(hours=30),
        )
        self.url = reverse("inbox_thread", args=[self.conversation.pk])

    def poll(self, shown):
        params = {} if shown is None else {"window_open": shown}
        return self.client.get(self.url, params).content.decode()

    def reply(self, body="Sí, me interesa"):
        # Through the same service the webhook feeds, so the window opens
        # exactly the way a real WhatsApp reply opens it.
        messaging_services.process_inbound_events([InboundEvent(
            event_type="message", provider_message_id="wamid.REPLY",
            from_number=self.contact.phone, body=body,
        )])

    def test_the_thread_reports_which_composer_it_shows(self):
        html = self.client.get(
            reverse("inbox_chat", args=[self.conversation.pk])
        ).content.decode()
        self.assertIn('hx-include="#chat-window-state"', html)
        self.assertIn('id="chat-window-state" name="window_open" value="0"', html)

    def test_a_reply_unlocks_the_composer_on_the_next_poll(self):
        template()
        self.reply()
        html = self.poll("0")
        self.assertIn("Sí, me interesa", html)          # the message list, as before
        self.assertIn('id="chat-composer" hx-swap-oob="outerHTML"', html)
        self.assertIn('name="body"', html)               # free text is back
        self.assertIn('id="chat-window-state" name="window_open" value="1"', html)

    def test_the_window_running_out_locks_the_composer(self):
        template()
        html = self.poll("1")   # the page still shows free text; 30h have passed
        self.assertIn('id="chat-composer" hx-swap-oob="outerHTML"', html)
        self.assertIn("ventana de 24 horas", html)
        self.assertNotIn('name="body"', html)
        self.assertIn('id="chat-window-state" name="window_open" value="0"', html)

    def test_an_unchanged_window_leaves_the_composer_alone(self):
        self.assertNotIn("chat-composer", self.poll("0"))
        self.reply()
        self.assertNotIn("chat-composer", self.poll("1"))

    def test_a_poll_that_reports_nothing_gets_only_the_messages(self):
        self.reply()
        html = self.poll(None)
        self.assertNotIn("chat-composer", html)
        self.assertIn("Sí, me interesa", html)

    def test_anything_else_in_the_parameter_is_ignored(self):
        self.reply()
        self.assertNotIn("chat-composer", self.poll("yes"))


class TemplateVariablesTests(TestCase):
    """The plantilla's {{n}} are filled by the agent, per customer. Sending
    the editor's sample values would greet every customer as "Camila"."""

    URL = reverse("inbox_new_chat")

    def test_the_modal_offers_an_input_per_variable_prefilled_with_the_sample(self):
        tpl = template(body="Hola {{1}}, tu pedido {{2}}.", samples=("Camila", "#4512"))
        html = self.client.get(self.URL, {"plantilla": tpl.pk}).content.decode()
        self.assertIn(f'name="var_{tpl.pk}_1"', html)
        self.assertIn(f'name="var_{tpl.pk}_2"', html)
        self.assertIn('value="Camila"', html)   # the sample, as a starting point

    def test_a_blank_variable_is_refused_rather_than_sent(self):
        camila = client()
        tpl = template(body="Hola {{1}}.", samples=("Camila",))
        html = self.client.post(
            self.URL, {"cliente": camila.pk, "plantilla": tpl.pk, f"var_{tpl.pk}_1": "  "}
        ).content.decode()
        self.assertIn("Completa todas las variables", html)
        self.assertEqual(Message.objects.count(), 0)

    def test_what_the_agent_typed_is_what_the_customer_receives(self):
        pedro = client("Pedro", "+573000000999")
        tpl = template(body="Hola {{1}}, tu pedido {{2}}.", samples=("Camila", "#4512"))
        self.client.post(
            self.URL,
            {"cliente": pedro.pk, "plantilla": tpl.pk,
             f"var_{tpl.pk}_1": "Pedro", f"var_{tpl.pk}_2": "#9001"},
        )
        self.assertEqual(Message.objects.get().body, "Hola Pedro, tu pedido #9001.")
