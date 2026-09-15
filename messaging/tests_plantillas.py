"""Tests for the plantilla catalogue: submitting one to the provider
(services.submit_template), reading approval verdicts back
(services.sync_template_verdicts), and the Meta Graph calls behind both --
mocked at ``requests``, since Meta reviews templates on its own clock and
there is no webhook for the verdict.

Sending a plantilla is a separate path with its own tests; this file is only
about the catalogue Meta keeps and how the CRM stays in step with it."""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from core.models import Client, MessageTemplate
from messaging import services
from messaging.models import Conversation, Message
from messaging.providers.base import MessagingProvider
from messaging.providers.fake import FakeProvider
from messaging.providers.meta import MetaProvider
from messaging.providers.types import TemplateSpec, TemplateStatus, TemplateVerdict


def plantilla(name="pedido_listo", body="Hola {{1}}, tu pedido {{2}} está listo.",
              samples=("Ana", "#4512"), status="aceptada", **kwargs):
    return MessageTemplate.objects.create(
        name=name, body=body, body_sample_values=list(samples), status=status, **kwargs
    )


def conversation(hours_since_inbound=48):
    """A WhatsApp chat whose 24h window closed a day ago -- the case template
    sends exist for. ``hours_since_inbound=1`` keeps it open."""
    from django.utils import timezone

    contact = Client.objects.create(first_name="Ana", last_name="Test", phone="+573000000777")
    return Conversation.objects.create(
        contact=contact,
        channel="whatsapp",
        last_inbound_at=timezone.now() - timedelta(hours=hours_since_inbound),
    )


def graph_response(payload, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    response.text = str(payload)
    response.raise_for_status.return_value = None
    return response


class SubmitTemplateTests(TestCase):
    def test_a_provider_without_a_catalogue_is_a_no_op(self):
        entry = plantilla(status="pendiente")
        self.assertFalse(services.submit_template(entry))
        entry.refresh_from_db()
        self.assertEqual(entry.provider_template_id, "")

    def test_a_returned_id_is_recorded_and_status_reset_to_pendiente(self):
        entry = plantilla(status="aceptada")
        with patch.object(FakeProvider, "create_template", return_value="12345"):
            self.assertTrue(services.submit_template(entry))
        entry.refresh_from_db()
        self.assertEqual(entry.provider_template_id, "12345")
        self.assertEqual(entry.status, "pendiente")

    def test_a_provider_error_becomes_a_submission_failure(self):
        entry = plantilla()
        with patch.object(FakeProvider, "create_template", side_effect=RuntimeError("nope")):
            with self.assertRaises(services.TemplateSubmissionFailed) as caught:
                services.submit_template(entry)
        self.assertIn("nope", str(caught.exception))
        # The row is untouched -- the editor's work survives the hiccup.
        entry.refresh_from_db()
        self.assertEqual(entry.provider_template_id, "")

    def test_the_spec_handed_over_mirrors_the_row(self):
        entry = plantilla(
            header_type="text", header_text="Tu pedido", footer="Gracias",
            buttons=[{"type": "quick_reply", "text": "Ok"}], category="utility",
        )
        with patch.object(FakeProvider, "create_template", return_value="1") as create:
            services.submit_template(entry)
        spec = create.call_args.args[0]
        self.assertIsInstance(spec, TemplateSpec)
        self.assertEqual(spec.name, "pedido_listo")
        self.assertEqual(spec.category, "utility")
        self.assertEqual(spec.header_text, "Tu pedido")
        self.assertEqual(spec.body_sample_values, ["Ana", "#4512"])
        self.assertEqual(spec.buttons, [{"type": "quick_reply", "text": "Ok"}])
        self.assertIsNone(spec.header_media)


class SyncTemplateVerdictsTests(TestCase):
    def verdicts(self, *entries):
        return patch.object(FakeProvider, "template_verdicts", return_value=list(entries))

    def test_a_provider_without_a_catalogue_changes_nothing(self):
        plantilla(status="pendiente")
        self.assertEqual(services.sync_template_verdicts(), 0)

    def test_an_approval_lands_on_the_matching_row(self):
        entry = plantilla(status="pendiente")
        with self.verdicts(TemplateVerdict("pedido_listo", "es", TemplateStatus.APPROVED,
                                           provider_template_id="777")):
            self.assertEqual(services.sync_template_verdicts(), 1)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "aceptada")
        self.assertEqual(entry.provider_template_id, "777")
        self.assertIsNotNone(entry.status_synced_at)

    def test_a_rejection_brings_its_reason(self):
        entry = plantilla(status="pendiente")
        with self.verdicts(TemplateVerdict("pedido_listo", "es", TemplateStatus.REJECTED,
                                           rejection_reason="INVALID_FORMAT")):
            services.sync_template_verdicts()
        entry.refresh_from_db()
        self.assertEqual(entry.status, "rechazada")
        self.assertEqual(entry.rejection_reason, "INVALID_FORMAT")

    def test_matching_is_per_language(self):
        es = plantilla(status="pendiente", language="es")
        en = plantilla(status="pendiente", language="en")
        with self.verdicts(TemplateVerdict("pedido_listo", "en", TemplateStatus.APPROVED)):
            services.sync_template_verdicts()
        es.refresh_from_db(); en.refresh_from_db()
        self.assertEqual(es.status, "pendiente")
        self.assertEqual(en.status, "aceptada")

    def test_rows_the_provider_does_not_know_are_left_alone(self):
        # Absence is not a verdict.
        entry = plantilla(name="solo_local", status="aceptada")
        with self.verdicts(TemplateVerdict("otra", "es", TemplateStatus.REJECTED)):
            self.assertEqual(services.sync_template_verdicts(), 0)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "aceptada")
        self.assertIsNone(entry.status_synced_at)

    def test_an_unchanged_row_still_gets_its_sync_time(self):
        entry = plantilla(status="aceptada")
        with self.verdicts(TemplateVerdict("pedido_listo", "es", TemplateStatus.APPROVED)):
            self.assertEqual(services.sync_template_verdicts(), 0)
        entry.refresh_from_db()
        self.assertIsNotNone(entry.status_synced_at)


class ProviderDefaultsTests(TestCase):
    def test_the_base_class_has_no_catalogue(self):
        fake = FakeProvider()
        self.assertIsNone(fake.create_template(TemplateSpec("x", "es", "marketing", "hola")))
        self.assertEqual(fake.template_verdicts(), [])
        self.assertIs(type(fake).template_verdicts, MessagingProvider.template_verdicts)


@override_settings(
    META_ACCESS_TOKEN="test-token",
    META_WABA_ID="10203040",
    META_APP_ID="55667788",
    MESSAGING_PROVIDER="meta",
)
class MetaTemplateCatalogueTests(TestCase):
    """MetaProvider.create_template / template_verdicts: the Graph payloads
    and the normalization of what comes back."""

    def setUp(self):
        self.provider = MetaProvider()

    def spec(self, **overrides):
        base = dict(
            name="pedido_listo", language="es", category="utility",
            body="Hola {{1}}, tu pedido {{2}} está listo.",
            body_sample_values=["Ana", "#4512"],
        )
        base.update(overrides)
        return TemplateSpec(**base)

    # --- create_template ----------------------------------------------------

    @patch("messaging.providers.meta.requests.post")
    def test_create_posts_to_the_waba_and_returns_the_id(self, mock_post):
        mock_post.return_value = graph_response({"id": "9001", "status": "PENDING"})

        template_id = self.provider.create_template(self.spec())

        self.assertEqual(template_id, "9001")
        url = mock_post.call_args.args[0]
        self.assertIn("/10203040/message_templates", url)
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-token")

    @patch("messaging.providers.meta.requests.post")
    def test_create_builds_body_with_examples_and_uppercase_category(self, mock_post):
        mock_post.return_value = graph_response({"id": "1"})

        self.provider.create_template(self.spec())

        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["name"], "pedido_listo")
        self.assertEqual(sent["language"], "es")
        self.assertEqual(sent["category"], "UTILITY")
        body = [c for c in sent["components"] if c["type"] == "BODY"][0]
        self.assertEqual(body["text"], "Hola {{1}}, tu pedido {{2}} está listo.")
        self.assertEqual(body["example"], {"body_text": [["Ana", "#4512"]]})

    @patch("messaging.providers.meta.requests.post")
    def test_create_maps_text_header_footer_and_every_button_kind(self, mock_post):
        mock_post.return_value = graph_response({"id": "1"})

        self.provider.create_template(self.spec(
            header_type="text", header_text="Tu pedido", footer="Gracias por comprar",
            buttons=[
                {"type": "quick_reply", "text": "Sí"},
                {"type": "url", "text": "Ver", "url": "https://tienda.example/p/1"},
                {"type": "phone", "text": "Llamar", "phone": "+573001112233"},
            ],
        ))

        components = {c["type"]: c for c in mock_post.call_args.kwargs["json"]["components"]}
        self.assertEqual(components["HEADER"], {"type": "HEADER", "format": "TEXT", "text": "Tu pedido"})
        self.assertEqual(components["FOOTER"]["text"], "Gracias por comprar")
        self.assertEqual(components["BUTTONS"]["buttons"], [
            {"type": "QUICK_REPLY", "text": "Sí"},
            {"type": "URL", "text": "Ver", "url": "https://tienda.example/p/1"},
            {"type": "PHONE_NUMBER", "text": "Llamar", "phone_number": "+573001112233"},
        ])

    @patch("messaging.providers.meta.requests.post")
    def test_create_without_variables_ships_no_example(self, mock_post):
        mock_post.return_value = graph_response({"id": "1"})
        self.provider.create_template(self.spec(body="Gracias.", body_sample_values=[]))
        body = mock_post.call_args.kwargs["json"]["components"][0]
        self.assertNotIn("example", body)

    @patch("messaging.providers.meta.requests.post")
    def test_a_media_header_goes_through_the_resumable_upload_first(self, mock_post):
        # Three POSTs in order: open an upload session, push the bytes, then
        # the template itself carrying the handle.
        mock_post.side_effect = [
            graph_response({"id": "upload:SESSION"}),
            graph_response({"h": "4:HANDLE"}),
            graph_response({"id": "9002"}),
        ]
        media = SimpleUploadedFile("muestra.png", b"\x89PNGbytes", content_type="image/png")

        template_id = self.provider.create_template(self.spec(header_type="image", header_media=media))

        self.assertEqual(template_id, "9002")
        session_call, upload_call, create_call = mock_post.call_args_list
        self.assertIn("/55667788/uploads", session_call.args[0])
        self.assertEqual(session_call.kwargs["params"]["file_length"], len(b"\x89PNGbytes"))
        self.assertEqual(session_call.kwargs["params"]["file_type"], "image/png")
        self.assertIn("/upload:SESSION", upload_call.args[0])
        self.assertEqual(upload_call.kwargs["data"], b"\x89PNGbytes")
        self.assertEqual(upload_call.kwargs["headers"]["Authorization"], "OAuth test-token")
        self.assertEqual(upload_call.kwargs["headers"]["file_offset"], "0")
        header = create_call.kwargs["json"]["components"][0]
        self.assertEqual(header, {"type": "HEADER", "format": "IMAGE",
                                  "example": {"header_handle": ["4:HANDLE"]}})

    @override_settings(META_APP_ID="")
    def test_a_media_header_without_an_app_id_fails_loudly(self):
        media = SimpleUploadedFile("muestra.png", b"x", content_type="image/png")
        with self.assertRaises(RuntimeError):
            self.provider.create_template(self.spec(header_type="image", header_media=media))

    @override_settings(META_WABA_ID="")
    def test_create_without_a_waba_id_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            self.provider.create_template(self.spec())

    @patch("messaging.providers.meta.requests.post")
    def test_create_surfaces_graph_errors(self, mock_post):
        import requests

        response = graph_response({"error": {"message": "name taken"}}, status=400)
        mock_post.return_value = response
        with self.assertRaises(requests.HTTPError) as caught:
            self.provider.create_template(self.spec())
        self.assertIn("name taken", str(caught.exception))

    @patch("messaging.providers.meta.requests.post")
    def test_create_prefers_metas_own_wording_for_a_person(self, mock_post):
        """``raise_for_status`` renders as "400 Client Error: Bad Request for
        url: ...", which tells the person who pressed Crear plantilla
        nothing. The reason is in the body Graph sent, and the Plantillas
        page shows ``str(exc)`` verbatim -- so it has to travel in there."""
        import requests

        mock_post.return_value = graph_response(
            {
                "error": {
                    "message": "(#100) Invalid parameter",
                    "error_user_title": "Nombre de plantilla duplicado",
                    "error_user_msg": "Ya existe una plantilla con ese nombre.",
                }
            },
            status=400,
        )
        with self.assertRaises(requests.HTTPError) as caught:
            self.provider.create_template(self.spec())
        message = str(caught.exception)
        self.assertIn("Nombre de plantilla duplicado", message)
        self.assertIn("Ya existe una plantilla con ese nombre.", message)

    @patch("messaging.providers.meta.requests.post")
    def test_a_body_graph_did_not_write_still_names_the_status(self, mock_post):
        """A proxy's HTML error page, or an empty body: no reason to quote,
        but the status code must still reach the caller."""
        import requests

        response = graph_response({}, status=502)
        response.json.side_effect = ValueError("not json")
        mock_post.return_value = response
        with self.assertRaises(requests.HTTPError) as caught:
            self.provider.create_template(self.spec())
        self.assertIn("502", str(caught.exception))

    # --- authentication templates -------------------------------------------

    @patch("messaging.providers.meta.requests.post")
    def test_an_authentication_template_ships_metas_fixed_shape(self, mock_post):
        """Meta writes an authentication template's copy itself and refuses
        one that arrives with its own BODY text -- which is what this used to
        send, so every plantilla created under Autenticación was rejected."""
        mock_post.return_value = graph_response({"id": "9002"})

        self.provider.create_template(
            self.spec(
                category="authentication",
                body="Tu código de verificación es {{1}}.",
                body_sample_values=["123456"],
                auth_security_recommendation=True,
                auth_code_expiration_minutes=10,
                auth_button_text="Copiar código",
            )
        )

        components = mock_post.call_args.kwargs["json"]["components"]
        by_type = {component["type"]: component for component in components}
        self.assertEqual(mock_post.call_args.kwargs["json"]["category"], "AUTHENTICATION")
        # The body carries the flag and no text of ours.
        self.assertEqual(
            by_type["BODY"], {"type": "BODY", "add_security_recommendation": True}
        )
        self.assertNotIn("text", by_type["BODY"])
        self.assertEqual(by_type["FOOTER"]["code_expiration_minutes"], 10)
        self.assertEqual(
            by_type["BUTTONS"]["buttons"],
            [{"type": "OTP", "otp_type": "COPY_CODE", "text": "Copiar código"}],
        )

    @patch("messaging.providers.meta.requests.post")
    def test_an_authentication_template_omits_what_was_not_asked_for(self, mock_post):
        mock_post.return_value = graph_response({"id": "9003"})

        self.provider.create_template(
            self.spec(
                category="authentication",
                auth_security_recommendation=False,
                auth_code_expiration_minutes=None,
            )
        )

        components = mock_post.call_args.kwargs["json"]["components"]
        by_type = {component["type"]: component for component in components}
        self.assertEqual(by_type["BODY"], {"type": "BODY"})
        self.assertNotIn("FOOTER", by_type)
        # The OTP button is not optional -- Meta requires one.
        self.assertEqual(by_type["BUTTONS"]["buttons"][0]["text"], "Copiar código")

    @patch("messaging.providers.meta.requests.post")
    def test_a_non_authentication_template_is_untouched(self, mock_post):
        mock_post.return_value = graph_response({"id": "9004"})

        self.provider.create_template(self.spec())

        components = mock_post.call_args.kwargs["json"]["components"]
        body = next(c for c in components if c["type"] == "BODY")
        self.assertEqual(body["text"], "Hola {{1}}, tu pedido {{2}} está listo.")
        self.assertEqual(body["example"], {"body_text": [["Ana", "#4512"]]})

    # --- template_verdicts --------------------------------------------------

    @patch("messaging.providers.meta.requests.get")
    def test_verdicts_normalize_meta_statuses(self, mock_get):
        mock_get.return_value = graph_response({"data": [
            {"id": "1", "name": "a", "language": "es", "status": "APPROVED"},
            {"id": "2", "name": "b", "language": "es", "status": "PENDING"},
            {"id": "3", "name": "c", "language": "es", "status": "REJECTED",
             "rejected_reason": "INVALID_FORMAT"},
            {"id": "4", "name": "d", "language": "es", "status": "PAUSED"},
            {"id": "5", "name": "e", "language": "es", "status": "IN_APPEAL"},
            {"id": "6", "name": "f", "language": "es", "status": "SOMETHING_NEW"},
        ]})

        verdicts = {v.name: v for v in self.provider.template_verdicts()}

        self.assertEqual(verdicts["a"].status, TemplateStatus.APPROVED)
        self.assertEqual(verdicts["b"].status, TemplateStatus.PENDING)
        self.assertEqual(verdicts["c"].status, TemplateStatus.REJECTED)
        self.assertEqual(verdicts["c"].rejection_reason, "INVALID_FORMAT")
        # Paused: rejected for our purposes, with the Meta state as the why.
        self.assertEqual(verdicts["d"].status, TemplateStatus.REJECTED)
        self.assertEqual(verdicts["d"].rejection_reason, "PAUSED")
        self.assertEqual(verdicts["e"].status, TemplateStatus.PENDING)
        self.assertNotIn("f", verdicts)  # unknown: skipped, never guessed
        self.assertEqual(verdicts["a"].provider_template_id, "1")

    @patch("messaging.providers.meta.requests.get")
    def test_verdicts_drop_metas_none_reason(self, mock_get):
        mock_get.return_value = graph_response({"data": [
            {"id": "1", "name": "a", "language": "es", "status": "APPROVED",
             "rejected_reason": "NONE"},
        ]})
        self.assertEqual(self.provider.template_verdicts()[0].rejection_reason, "")

    @patch("messaging.providers.meta.requests.get")
    def test_verdicts_follow_paging(self, mock_get):
        mock_get.side_effect = [
            graph_response({
                "data": [{"id": "1", "name": "a", "language": "es", "status": "APPROVED"}],
                "paging": {"next": "https://graph.facebook.com/next-page"},
            }),
            graph_response({
                "data": [{"id": "2", "name": "b", "language": "es", "status": "APPROVED"}],
            }),
        ]
        names = [v.name for v in self.provider.template_verdicts()]
        self.assertEqual(names, ["a", "b"])
        second_call = mock_get.call_args_list[1]
        self.assertEqual(second_call.args[0], "https://graph.facebook.com/next-page")
        self.assertIsNone(second_call.kwargs["params"])

    @patch("messaging.providers.meta.requests.get")
    def test_verdicts_ask_for_the_fields_the_sync_needs(self, mock_get):
        mock_get.return_value = graph_response({"data": []})
        self.provider.template_verdicts()
        params = mock_get.call_args.kwargs["params"]
        for field in ["name", "language", "status", "rejected_reason", "id"]:
            self.assertIn(field, params["fields"])
        self.assertIn("/10203040/message_templates", mock_get.call_args.args[0])


class SendTemplateTests(TestCase):
    """services.send_template against the fake provider (the default)."""

    def test_sends_outside_the_24h_window(self):
        # The whole point: send_message would raise SendWindowClosed here.
        chat = conversation(hours_since_inbound=48)
        message = services.send_template(chat, plantilla(), {"1": "Ana", "2": "#4512"})
        self.assertEqual(message.direction, Message.OUTBOUND)
        self.assertTrue(message.provider_message_id.startswith("fake-"))

    def test_stores_the_rendered_text_not_the_template_name(self):
        chat = conversation()
        message = services.send_template(chat, plantilla(), {"1": "Ana", "2": "#4512"})
        self.assertEqual(message.body, "Hola Ana, tu pedido #4512 está listo.")

    def test_passes_name_values_and_language_to_the_provider(self):
        chat = conversation()
        entry = plantilla(language="es_MX")
        with patch.object(FakeProvider, "send_template", return_value="fake-x") as send:
            services.send_template(chat, entry, {"2": "#4512", "1": "Ana"})
        send.assert_called_once_with(
            to="+573000000777",
            template_name="pedido_listo",
            # _rendered rides along for providers with no template catalogue,
            # which would otherwise send the template's NAME.
            params={
                "2": "#4512",
                "1": "Ana",
                "_language": "es_MX",
                "_rendered": "Hola Ana, tu pedido #4512 está listo.",
                # Only authentication does anything with this, but the
                # provider is the one that knows that.
                "_category": "marketing",
            },
        )

    def test_bumps_the_conversation_and_records_the_sender(self):
        from django.contrib.auth import get_user_model

        chat = conversation()
        user = get_user_model().objects.create_user("ana", password="x" * 12)
        message = services.send_template(chat, plantilla(), {"1": "Ana", "2": "#1"}, user)
        chat.refresh_from_db()
        self.assertEqual(chat.last_message_at, message.timestamp)
        self.assertEqual(message.sent_by, user)

    def test_a_rejected_plantilla_is_refused_before_any_row_exists(self):
        chat = conversation()
        with self.assertRaises(services.TemplateNotSendable):
            services.send_template(chat, plantilla(status="rechazada"), {"1": "a", "2": "b"})
        self.assertEqual(Message.objects.count(), 0)

    def test_an_inactive_plantilla_is_refused(self):
        chat = conversation()
        with self.assertRaises(services.TemplateNotSendable):
            services.send_template(chat, plantilla(is_active=False), {"1": "a", "2": "b"})

    def test_a_pendiente_plantilla_goes_through(self):
        # No approval pipeline of our own -- the provider decides.
        chat = conversation()
        message = services.send_template(chat, plantilla(status="pendiente"), {"1": "a", "2": "b"})
        self.assertIsNotNone(message.pk)

    def test_a_provider_error_keeps_the_row_as_failed(self):
        chat = conversation()
        with patch.object(FakeProvider, "send_template", side_effect=RuntimeError("boom")):
            with self.assertRaises(services.SendFailed):
                services.send_template(chat, plantilla(), {"1": "a", "2": "b"})
        message = Message.objects.get()
        self.assertEqual(message.status, "failed")
        self.assertIsNone(message.provider_message_id)
