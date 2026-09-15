"""Tests for the plantilla lifecycle as the UI exposes it: the Inbox's
Enviar plantilla dialog (both composer states, the send, the rejections), the
Plantillas page's "Sincronizar con WhatsApp" button and its notices, and the
editor handing a freshly saved plantilla to the provider."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Client, MessageTemplate
from messaging import services
from messaging.models import Conversation, Message
from messaging.providers.types import TemplateStatus, TemplateVerdict
from messaging.testing import StubProvider

HTMX = {"HX-Request": "true"}


def plantilla(name="pedido_listo", body="Hola {{1}}, tu pedido {{2}} está listo.",
              samples=("Ana", "#4512"), status="aceptada", **kwargs):
    return MessageTemplate.objects.create(
        name=name, body=body, body_sample_values=list(samples), status=status, **kwargs
    )


class ConversationMixin:
    def make_conversation(self, hours_since_inbound=48):
        contact = Client.objects.create(
            first_name="Ana", last_name="Test", phone="+573000000777"
        )
        return Conversation.objects.create(
            contact=contact,
            channel="whatsapp",
            last_inbound_at=timezone.now() - timedelta(hours=hours_since_inbound),
        )


class ComposerButtonTests(ConversationMixin, TestCase):
    def test_the_closed_window_offers_enviar_plantilla(self):
        chat = self.make_conversation(hours_since_inbound=48)
        response = self.client.get(reverse("inbox_chat", args=[chat.pk]))
        self.assertContains(response, "ventana de 24 horas")
        self.assertContains(response, "Enviar plantilla")
        self.assertContains(response, reverse("inbox_template_send", args=[chat.pk]))
        # Still no free-form input: that send would bounce.
        self.assertNotContains(response, "composer__input")

    def test_the_open_window_offers_it_too(self):
        # A template message is not the same as its wording pasted in.
        chat = self.make_conversation(hours_since_inbound=1)
        response = self.client.get(reverse("inbox_chat", args=[chat.pk]))
        self.assertContains(response, "composer__input")
        self.assertContains(response, "Enviar plantilla")

    def test_the_dialog_has_a_slot_to_land_in(self):
        chat = self.make_conversation()
        response = self.client.get(reverse("inbox_chat", args=[chat.pk]))
        self.assertContains(response, 'id="tpl-send-slot"')


class TemplateSendDialogTests(ConversationMixin, TestCase):
    def setUp(self):
        self.chat = self.make_conversation()
        self.url = reverse("inbox_template_send", args=[self.chat.pk])

    def test_get_renders_the_dialog_with_sendable_plantillas(self):
        plantilla(name="pedido_listo")
        plantilla(name="apagada", is_active=False)
        plantilla(name="rechazada_ya", status="rechazada")
        html = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn('id="tpl-send-dialog"', html)
        self.assertIn("data-open-on-swap", html)
        self.assertIn("pedido_listo", html)
        self.assertNotIn("apagada", html)
        self.assertNotIn("rechazada_ya", html)

    def test_variables_are_prefilled_with_the_editor_samples(self):
        entry = plantilla()
        html = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn(f'name="var_{entry.pk}_1"', html)
        self.assertIn('value="Ana"', html)
        self.assertIn('value="#4512"', html)
        # The body travels to the client for the live preview.
        self.assertIn("data-template-body=", html)

    def test_the_empty_state_links_to_the_plantillas_page(self):
        html = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn("No hay plantillas activas", html)
        self.assertIn("view=plantillas-whatsapp", html)
        # Nothing to submit, so no submit button.
        self.assertNotIn('type="submit"', html)

    def test_a_valid_post_sends_and_answers_with_the_thread(self):
        entry = plantilla()
        response = self.client.post(
            self.url,
            {"template": entry.pk, f"var_{entry.pk}_1": "Ana", f"var_{entry.pk}_2": "#9"},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 200)
        message = Message.objects.get()
        self.assertEqual(message.body, "Hola Ana, tu pedido #9 está listo.")
        self.assertContains(response, "Hola Ana, tu pedido #9 está listo.")

    def test_a_blank_variable_is_rejected_into_the_dialog(self):
        entry = plantilla()
        response = self.client.post(
            self.url,
            {"template": entry.pk, f"var_{entry.pk}_1": "Ana", f"var_{entry.pk}_2": ""},
            headers=HTMX,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response["HX-Retarget"], "#tpl-send-body")
        self.assertContains(response, "Completa todas las variables", status_code=422)
        # What was typed comes back -- and the blank stays blank, rather than
        # being refilled with the sample the error is complaining about.
        self.assertContains(response, 'value="Ana"', status_code=422)
        self.assertNotContains(response, "#4512", status_code=422)
        self.assertEqual(Message.objects.count(), 0)

    def test_an_unknown_plantilla_is_rejected(self):
        response = self.client.post(self.url, {"template": 999}, headers=HTMX)
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Elige una plantilla", status_code=422)

    def test_a_plantilla_without_variables_needs_none(self):
        entry = plantilla(body="Gracias por tu compra.", samples=())
        response = self.client.post(self.url, {"template": entry.pk}, headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.get().body, "Gracias por tu compra.")

    def test_a_provider_failure_shows_in_the_thread_like_a_normal_send(self):
        entry = plantilla(body="Hola.", samples=())
        with patch.object(StubProvider, "send_template", side_effect=RuntimeError("down")):
            response = self.client.post(self.url, {"template": entry.pk}, headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No se pudo enviar la plantilla")
        self.assertEqual(Message.objects.get().status, "failed")

    def test_other_methods_are_not_allowed(self):
        self.assertEqual(self.client.put(self.url).status_code, 405)


class TemplateSendApprovalTests(ConversationMixin, TestCase):
    """Only aceptadas go out: Meta treats any other name as nonexistent
    (132001, "template name (prueba_texto) does not exist in es")."""

    def setUp(self):
        self.chat = self.make_conversation()
        self.url = reverse("inbox_template_send", args=[self.chat.pk])

    def test_only_aceptadas_are_offered(self):
        plantilla(name="pedido_listo")
        plantilla(name="prueba_texto", status="pendiente")
        html = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn("pedido_listo", html)
        self.assertNotIn("prueba_texto", html)

    def test_only_pendientes_explains_the_wait_and_points_at_the_sync(self):
        plantilla(name="prueba_texto", status="pendiente")
        html = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn("1 plantilla en revisión de Meta", html)
        self.assertIn("Sincronizar con WhatsApp", html)
        self.assertIn("view=plantillas-whatsapp", html)
        self.assertNotIn("No hay plantillas activas", html)
        self.assertNotIn('type="submit"', html)

    def test_rechazadas_and_inactive_pendientes_are_not_awaiting_review(self):
        plantilla(name="mala", status="rechazada")
        plantilla(name="apagada", status="pendiente", is_active=False)
        html = self.client.get(self.url, headers=HTMX).content.decode()
        self.assertIn("No hay plantillas activas", html)
        self.assertNotIn("en revisión de Meta", html)

    def test_a_crafted_post_for_a_pendiente_is_refused_before_meta_hears_of_it(self):
        entry = plantilla(name="prueba_texto", status="pendiente")
        with patch.object(StubProvider, "send_template") as send:
            response = self.client.post(self.url, {"template": entry.pk}, headers=HTMX)
        send.assert_not_called()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response["HX-Retarget"], "#tpl-send-body")
        self.assertContains(
            response, "«prueba_texto» sigue en revisión de Meta", status_code=422
        )
        # The reason comes before the blanks: filling them would not help.
        self.assertNotContains(response, "Completa todas las variables", status_code=422)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_crafted_post_for_a_rechazada_says_it_was_rejected(self):
        entry = plantilla(status="rechazada")
        response = self.client.post(self.url, {"template": entry.pk}, headers=HTMX)
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "WhatsApp rechazó esta plantilla", status_code=422)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_refusal_comes_back_on_a_plantilla_that_is_on_offer(self):
        on_offer = plantilla(name="pedido_listo")
        pending = plantilla(name="prueba_texto", status="pendiente")
        response = self.client.post(self.url, {"template": pending.pk}, headers=HTMX)
        self.assertContains(response, f'<option value="{on_offer.pk}" selected>', status_code=422)

    def test_an_aceptada_still_goes_out(self):
        entry = plantilla(body="Hola.", samples=())
        with patch.object(StubProvider, "send_template", return_value="wamid.OK") as send:
            response = self.client.post(self.url, {"template": entry.pk}, headers=HTMX)
        send.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.get().provider_message_id, "wamid.OK")


class PlantillasSyncTests(TestCase):
    def sync(self):
        return self.client.post(reverse("plantillas_sync"), headers=HTMX)

    def test_the_panel_has_the_sync_button(self):
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"view": "plantillas-whatsapp"}
        )
        self.assertContains(response, "Sincronizar con WhatsApp")
        self.assertContains(response, reverse("plantillas_sync"))

    def test_a_verdict_updates_the_row_and_reports_the_count(self):
        entry = plantilla(status="pendiente")
        with patch.object(StubProvider, "template_verdicts", return_value=[
            TemplateVerdict("pedido_listo", "es", TemplateStatus.APPROVED),
        ]):
            response = self.sync()
        entry.refresh_from_db()
        self.assertEqual(entry.status, "aceptada")
        self.assertContains(response, "1 plantilla(s) cambiaron")
        # The table came back re-rendered with the new Estado.
        self.assertContains(response, "Aceptada")

    def test_nothing_changed_reads_as_up_to_date(self):
        plantilla(status="aceptada")
        with patch.object(StubProvider, "template_verdicts", return_value=[
            TemplateVerdict("pedido_listo", "es", TemplateStatus.APPROVED),
        ]):
            response = self.sync()
        self.assertContains(response, "al día")

    def test_a_provider_error_is_reported_not_raised(self):
        with patch.object(StubProvider, "template_verdicts", side_effect=RuntimeError("401")):
            response = self.sync()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No se pudo consultar a WhatsApp")

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse("plantillas_sync")).status_code, 405)


class PlantillasTableStatusTests(TestCase):
    def table(self):
        return self.client.get(reverse("plantillas_table", args=["todas"]), headers=HTMX)

    def test_a_rejected_row_shows_why(self):
        plantilla(status="rechazada", rejection_reason="INVALID_FORMAT")
        self.assertContains(self.table(), "INVALID_FORMAT")

    def test_a_local_only_pendiente_says_it_was_never_submitted(self):
        plantilla(status="pendiente")
        self.assertContains(self.table(), "sin enviar a WhatsApp")

    def test_a_submitted_pendiente_does_not(self):
        plantilla(status="pendiente", provider_template_id="123")
        self.assertNotContains(self.table(), "sin enviar a WhatsApp")

    def test_the_sync_time_rides_on_the_estado_cell(self):
        plantilla(status="aceptada", status_synced_at=timezone.now())
        self.assertContains(self.table(), "Consultado a WhatsApp el")


class EditorSubmitsToProviderTests(TestCase):
    def payload(self):
        return {
            "name": "bienvenida_1", "category": "marketing", "sub_type": "custom",
            "language": "es", "team": "", "header_type": "none", "header_text": "",
            "body": "Hola, bienvenido.", "footer": "", "button_kind": "none",
        }

    def test_a_saved_plantilla_is_handed_to_the_provider(self):
        with patch.object(StubProvider, "create_template", return_value="4242") as create:
            self.client.post(reverse("plantilla_editor"), self.payload(), headers=HTMX)
        create.assert_called_once()
        entry = MessageTemplate.objects.get()
        self.assertEqual(entry.provider_template_id, "4242")

    def test_a_refused_submission_keeps_the_row_and_says_so(self):
        with patch.object(StubProvider, "create_template",
                          side_effect=RuntimeError("name already exists")):
            response = self.client.post(reverse("plantilla_editor"), self.payload(), headers=HTMX)
        self.assertEqual(MessageTemplate.objects.count(), 1)
        self.assertContains(response, "no la aceptó para revisión")
        self.assertContains(response, "name already exists")

    def test_a_successful_submission_is_quiet(self):
        response = self.client.post(reverse("plantilla_editor"), self.payload(), headers=HTMX)
        entry = MessageTemplate.objects.get()
        self.assertTrue(entry.provider_template_id.startswith("stub-tpl-"))
        self.assertNotContains(response, "tpl-notice")
