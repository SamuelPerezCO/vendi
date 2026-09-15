"""Tests for Respuestas rápidas: the rules in core.respuestas, the CRUD page
under Configuración de mensajería, the composer picker that lists them and
the one-click send (text, or image with caption) they trigger."""

from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import respuestas
from core.models import Client, QuickReply
from messaging import services as messaging_services
from messaging.models import Conversation, Message
from messaging.providers.base import MessagingProvider
from messaging.testing import StubProvider

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def png(name="foto.png", content_type="image/png", size=None):
    data = PNG if size is None else b"x" * size
    return SimpleUploadedFile(name, data, content_type=content_type)


def reply(title="Horario", body="Atendemos de 9 a 6.", **extra):
    return QuickReply.objects.create(title=title, body=body, **extra)


def conversation(hours_ago=1):
    contact = Client.objects.create(first_name="Camila", phone="+573000000777")
    return Conversation.objects.create(
        contact=contact,
        channel="whatsapp",
        last_inbound_at=timezone.now() - timedelta(hours=hours_ago),
    )


class ValidateTests(TestCase):
    def state(self, **overrides):
        data = {"title": "Horario", "body": "De 9 a 6.", "is_active": "1"}
        data.update(overrides)
        return respuestas.form_state(data)

    def test_a_titled_text_passes(self):
        self.assertEqual(respuestas.validate(self.state()), {})

    def test_the_title_is_required(self):
        self.assertIn("title", respuestas.validate(self.state(title="  ")))

    def test_it_needs_text_or_an_image(self):
        errors = respuestas.validate(self.state(body=""))
        self.assertIn("body", errors)
        # An image alone is a valid quick reply (a price list, say).
        self.assertNotIn("body", respuestas.validate(self.state(body=""), png()))

    def test_a_kept_image_counts_when_editing(self):
        existing = reply(image=png())
        self.assertNotIn("body", respuestas.validate(self.state(body=""), None, existing))
        # ...unless the edit removes it.
        removing = self.state(body="", remove_image="1")
        self.assertIn("body", respuestas.validate(removing, None, existing))

    def test_only_image_types_whatsapp_delivers_are_accepted(self):
        errors = respuestas.validate(self.state(), png("doc.pdf", "application/pdf"))
        self.assertIn("image", errors)
        for ok in ("image/jpeg", "image/png", "image/webp"):
            with self.subTest(ok):
                self.assertNotIn("image", respuestas.validate(self.state(), png("f", ok)))

    def test_an_oversized_image_is_refused(self):
        big = png(size=respuestas.IMAGE_MAX_BYTES + 1)
        self.assertIn("image", respuestas.validate(self.state(), big))

    def test_length_caps(self):
        self.assertIn("title", respuestas.validate(self.state(title="x" * 81)))
        self.assertIn("body", respuestas.validate(self.state(body="x" * 1025)))


class ApplyTests(TestCase):
    def test_creates_with_the_author(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create(username="sam")
        state = respuestas.form_state({"title": "Hola", "body": "Hola!", "is_active": "1"})
        saved = respuestas.apply(state, None, None, user)
        self.assertEqual(saved.created_by, user)
        self.assertTrue(saved.is_active)

    def test_replacing_the_image_keeps_the_old_file_for_sent_messages(self):
        # Message.media_url holds that URL; deleting the bytes would turn
        # already-sent history into broken images.
        existing = reply(image=png("old.png"))
        old_name = existing.image.name
        storage = existing.image.storage
        state = respuestas.form_state({"title": "Horario", "body": "x", "is_active": "1"})
        respuestas.apply(state, png("new.png"), existing)
        existing.refresh_from_db()
        self.assertNotEqual(existing.image.name, old_name)
        self.assertTrue(storage.exists(old_name))
        storage.delete(old_name)
        existing.image.delete(save=False)

    def test_remove_image_clears_the_field_but_not_the_file(self):
        existing = reply(image=png())
        name, storage = existing.image.name, existing.image.storage
        state = respuestas.form_state(
            {"title": "Horario", "body": "x", "is_active": "1", "remove_image": "1"}
        )
        respuestas.apply(state, None, existing)
        existing.refresh_from_db()
        self.assertFalse(existing.image)
        self.assertTrue(storage.exists(name))
        storage.delete(name)


class RespuestasPanelTests(TestCase):
    URL = reverse("section", args=["mensajeria"]) + "?view=respuestas-rapidas"

    def test_the_page_is_real_now(self):
        response = self.client.get(self.URL)
        self.assertContains(response, "+ Crear respuesta")
        self.assertNotContains(response, "próximamente")
        self.assertContains(response, 'id="reply-modal-body"')

    def test_empty_state_explains_the_feature(self):
        self.assertContains(self.client.get(self.URL), "Aún no hay respuestas rápidas")

    def test_rows_show_title_text_author_and_state(self):
        reply(title="Horario", body="De 9 a 6.")
        reply(title="Apagada", body="…", is_active=False)
        html = self.client.get(self.URL).content.decode()
        self.assertIn("Horario", html)
        self.assertIn("De 9 a 6.", html)
        self.assertIn("reply-row--off", html)     # inactive stays listed, muted
        self.assertLess(html.index("Horario"), html.index("Apagada"))

    def test_panel_endpoint_matches_the_page(self):
        fragment = self.client.get(
            reverse("mensajeria_panel", args=["respuestas-rapidas"])
        ).content.decode()
        self.assertIn("+ Crear respuesta", fragment)


class RespuestaCrudTests(TestCase):
    def test_create_form_renders(self):
        response = self.client.get(reverse("respuesta_create"))
        self.assertContains(response, "Crear respuesta rápida")
        self.assertContains(response, 'name="image"')
        self.assertContains(response, 'hx-encoding="multipart/form-data"')

    def test_create_saves_and_closes(self):
        response = self.client.post(
            reverse("respuesta_create"),
            {"title": "Horario", "body": "De 9 a 6.", "is_active": "1"},
        )
        self.assertContains(response, "data-dialog-dismiss")
        self.assertContains(response, 'id="reply-table" hx-swap-oob="innerHTML"')
        self.assertEqual(QuickReply.objects.get().title, "Horario")

    def test_create_with_an_image_stores_it(self):
        self.client.post(
            reverse("respuesta_create"),
            {"title": "Lista", "body": "", "is_active": "1", "image": png()},
        )
        saved = QuickReply.objects.get()
        self.assertTrue(saved.has_image)
        self.assertTrue(saved.image.name.startswith("respuestas/"))
        saved.image.delete(save=False)

    def test_a_rejected_create_keeps_the_dialog_open_with_errors(self):
        html = self.client.post(
            reverse("respuesta_create"), {"title": "", "body": ""}
        ).content.decode()
        self.assertNotIn("data-dialog-dismiss", html)
        self.assertIn("ffield--error", html)
        self.assertIn("Ponle un título", html)
        self.assertIn("algo hay que enviar", html)
        self.assertEqual(QuickReply.objects.count(), 0)

    def test_edit_prefills_and_saves(self):
        row = reply()
        html = self.client.get(reverse("respuesta_update", args=[row.pk])).content.decode()
        self.assertIn('value="Horario"', html)
        self.assertIn("Atendemos de 9 a 6.", html)
        self.assertIn("Eliminar", html)
        self.client.post(
            reverse("respuesta_update", args=[row.pk]),
            {"title": "Horario nuevo", "body": "De 8 a 5.", "is_active": "1"},
        )
        row.refresh_from_db()
        self.assertEqual((row.title, row.body), ("Horario nuevo", "De 8 a 5."))

    def test_unchecking_active_hides_it_from_the_picker(self):
        row = reply()
        self.client.post(
            reverse("respuesta_update", args=[row.pk]),
            {"title": "Horario", "body": "x"},   # no is_active -> off
        )
        row.refresh_from_db()
        self.assertFalse(row.is_active)

    def test_toggle_flips_the_switch_and_rerenders_the_table(self):
        row = reply()
        response = self.client.post(
            reverse("respuesta_toggle", args=[row.pk]), {"active": "0"}
        )
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertContains(response, "reply-row--off")

    def test_delete_asks_then_deletes_but_keeps_the_sent_image(self):
        row = reply(image=png())
        name = row.image.name
        storage = row.image.storage
        self.assertContains(
            self.client.get(reverse("respuesta_delete", args=[row.pk])), "Eliminar «Horario»"
        )
        self.assertEqual(QuickReply.objects.count(), 1)
        response = self.client.post(reverse("respuesta_delete", args=[row.pk]))
        self.assertContains(response, "data-dialog-dismiss")
        self.assertEqual(QuickReply.objects.count(), 0)
        # The file survives: threads that already show it must keep working.
        self.assertTrue(storage.exists(name))
        storage.delete(name)

    def test_unknown_reply_is_404(self):
        for name in ("respuesta_update", "respuesta_delete"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=[999])).status_code, 404)


class PickerTests(TestCase):
    def setUp(self):
        self.conversation = conversation()

    def get(self):
        return self.client.get(reverse("inbox_quick_replies", args=[self.conversation.pk]))

    def test_lists_active_quick_replies_not_plantillas(self):
        from core.models import MessageTemplate
        MessageTemplate.objects.create(name="una_plantilla", body="Hola", status="aceptada")
        reply(title="Horario")
        reply(title="Apagada", is_active=False)
        html = self.get().content.decode()
        self.assertIn("Horario", html)
        self.assertNotIn("Apagada", html)
        self.assertNotIn("una_plantilla", html)

    def test_each_entry_posts_its_id_into_the_open_chat(self):
        row = reply()
        html = self.get().content.decode()
        self.assertIn(f'hx-post="{reverse("inbox_send", args=[self.conversation.pk])}"', html)
        self.assertIn(f"""hx-vals='{{"quick_reply": "{row.pk}"}}'""", html)
        self.assertIn("data-quick-send", html)

    def test_an_entry_with_an_image_shows_a_thumbnail(self):
        row = reply(image=png())
        html = self.get().content.decode()
        self.assertIn("quickreplies__thumb", html)
        self.assertIn("Imagen", html)
        row.image.delete(save=False)

    def test_empty_state_links_to_the_config_page(self):
        html = self.get().content.decode()
        self.assertIn("No hay respuestas rápidas", html)
        self.assertIn("view=respuestas-rapidas", html)


class QuickSendTests(TestCase):
    def setUp(self):
        self.conversation = conversation()
        self.url = reverse("inbox_send", args=[self.conversation.pk])

    def test_posting_the_id_sends_the_text(self):
        row = reply(body="Atendemos de 9 a 6.")
        response = self.client.post(self.url, {"quick_reply": row.pk})
        self.assertContains(response, "Atendemos de 9 a 6.")
        message = Message.objects.get()
        self.assertEqual(message.body, "Atendemos de 9 a 6.")
        self.assertEqual(message.media_url, "")

    @override_settings(PUBLIC_BASE_URL="")
    def test_an_image_reply_sends_the_image_with_the_text_as_caption(self):
        row = reply(title="Lista", body="Precios de hoy", image=png())
        with mock.patch.object(StubProvider, "send_image", return_value="stub-img-1") as send:
            self.client.post(self.url, {"quick_reply": row.pk})
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["caption"], "Precios de hoy")
        self.assertTrue(kwargs["image_url"].endswith(row.image.name.split("/")[-1]))
        message = Message.objects.get()
        self.assertEqual(message.media_type, "image")
        # Absolute, not the relative path storage answers with. Meta fetches
        # this link from its own servers and rejects a bare path outright --
        # "(#100) Param image.link is not a valid URI" -- which reaches the
        # agent as a send that simply failed, well after they clicked.
        self.assertEqual(message.media_url, "http://testserver" + row.image.url)
        self.assertTrue(message.is_inline_image)   # the thread renders it
        row.image.delete(save=False)

    @override_settings(PUBLIC_BASE_URL="https://crm.example.com")
    def test_the_link_handed_to_the_provider_uses_the_configured_origin(self):
        """End to end through the view, not just image_url().

        The unit test on image_url() would pass while a second caller, or a
        change to the view, rebuilt the link from the request's Host -- which
        is exactly how four photo sends went to an SSO-protected alias and
        failed in one evening. This pins what provider.send_image actually
        receives, and what the thread stores, when an origin is configured.
        """
        row = reply(title="Promo", body="Foto", image=png())
        with mock.patch.object(StubProvider, "send_image", return_value="stub-img-2") as send:
            self.client.post(self.url, {"quick_reply": row.pk}, HTTP_HOST="testserver")
        expected = "https://crm.example.com" + row.image.url
        self.assertEqual(send.call_args.kwargs["image_url"], expected)
        self.assertEqual(Message.objects.get().media_url, expected)
        row.image.delete(save=False)

    def test_an_inactive_or_unknown_id_sends_nothing(self):
        off = reply(is_active=False)
        for value in (off.pk, 999999):
            with self.subTest(value):
                self.client.post(self.url, {"quick_reply": value})
        self.assertEqual(Message.objects.count(), 0)

    def test_the_id_wins_over_a_stray_body(self):
        # The picker never sends a body, but if one arrives the reply is
        # what gets sent -- the server resolves the text, not the client.
        row = reply(body="Texto real")
        self.client.post(self.url, {"quick_reply": row.pk, "body": "otra cosa"})
        self.assertEqual(Message.objects.get().body, "Texto real")

    def test_outside_the_window_the_send_is_refused_like_any_text(self):
        closed = conversation(hours_ago=30)
        row = reply()
        response = self.client.post(
            reverse("inbox_send", args=[closed.pk]), {"quick_reply": row.pk}
        )
        self.assertContains(response, "ventana de 24 horas")
        self.assertEqual(Message.objects.count(), 0)


class SendImageServiceTests(TestCase):
    def test_send_message_with_image_records_media_and_calls_send_image(self):
        chat = conversation()
        with mock.patch.object(StubProvider, "send_image", return_value="id-1") as send:
            message = messaging_services.send_message(
                chat, "pie", image_url="https://cdn.example/x.png"
            )
        send.assert_called_once_with(
            to="+573000000777", image_url="https://cdn.example/x.png", caption="pie"
        )
        self.assertEqual(message.media_url, "https://cdn.example/x.png")
        self.assertEqual(message.media_type, "image")
        self.assertEqual(message.provider_message_id, "id-1")

    def test_a_failed_image_send_keeps_the_row_as_failed(self):
        chat = conversation()
        with mock.patch.object(StubProvider, "send_image", side_effect=RuntimeError("boom")):
            with self.assertRaises(messaging_services.SendFailed):
                messaging_services.send_message(chat, "pie", image_url="https://x/y.png")
        self.assertEqual(Message.objects.get().status, "failed")

    def test_the_base_provider_falls_back_to_text(self):
        # A text-only backend still delivers the words.
        class TextOnly(MessagingProvider):
            name = "textonly"
            sent = []
            def send_text(self, to, body):
                self.sent.append((to, body)); return "t-1"
            def send_template(self, to, template_name, params): return "t-2"
            def parse_webhook(self, request): return []
            def verify_signature(self, request): return True
            def create_template(self, spec): return "tpl-1"
            def template_verdicts(self): return []

        provider = TextOnly()
        self.assertEqual(provider.send_image("+57", "https://x/y.png", "hola"), "t-1")
        self.assertEqual(provider.sent, [("+57", "hola")])
        provider.send_image("+57", "https://x/y.png")   # no caption -> the URL
        self.assertEqual(provider.sent[-1], ("+57", "https://x/y.png"))


class ImageUrlIsAbsoluteTests(TestCase):
    """core.respuestas.image_url must hand providers an absolute URL.

    Meta fetches the link from its own servers, so a relative path is a 400
    ("Param image.link is not a valid URI") -- and the agent only finds out
    after clicking send. Blob storage returned absolute CDN URLs, so nothing
    caught this until uploads moved into the database.
    """

    @override_settings(PUBLIC_BASE_URL="https://crm.example.com")
    def test_the_configured_public_origin_beats_the_requests_host(self):
        """The link's reader is Meta, not the agent.

        Deployment protection puts every alias except the production domain
        behind Vercel SSO. A signed-in agent reaches the app through any of
        them, so the request Host is whichever alias they logged in through
        -- and a link built from it sends Meta to a login page. Four sends
        went to ``failed`` that way in one evening while two from the
        production domain delivered, same reply, same file. The configured
        public origin must win even when a request is right there.
        """
        row = reply(title="Promo", body="hola", image=png())
        request = RequestFactory().get("/", HTTP_HOST="testserver")
        self.assertEqual(
            respuestas.image_url(row, request),
            "https://crm.example.com" + row.image.url,
        )
        row.image.delete(save=False)

    @override_settings(PUBLIC_BASE_URL="")
    def test_the_request_host_is_only_the_fallback_with_no_origin_configured(self):
        # Local development: Meta could not reach the link anyway, and there
        # is no production domain to point at.
        row = reply(title="Promo", body="hola", image=png())
        request = RequestFactory().get("/", HTTP_HOST="testserver")
        self.assertEqual(
            respuestas.image_url(row, request), "http://testserver" + row.image.url
        )
        row.image.delete(save=False)

    @override_settings(PUBLIC_BASE_URL="https://crm.example.com")
    def test_without_a_request_it_falls_back_to_the_configured_origin(self):
        # The path taken by anything sending outside a request cycle.
        row = reply(title="Promo", body="hola", image=png())
        self.assertEqual(
            respuestas.image_url(row), "https://crm.example.com" + row.image.url
        )
        row.image.delete(save=False)

    @override_settings(PUBLIC_BASE_URL="")
    def test_with_no_origin_at_all_it_sends_the_caption_rather_than_a_bad_link(self):
        # A relative link would be rejected by Meta and lose the whole message;
        # "" drops the photo but still delivers the text.
        row = reply(title="Promo", body="hola", image=png())
        self.assertEqual(respuestas.image_url(row), "")
        row.image.delete(save=False)

    def test_an_absolute_url_from_blob_storage_is_left_alone(self):
        row = reply(title="Promo", body="hola", image=png())
        with mock.patch.object(
            type(row.image), "url", new_callable=mock.PropertyMock
        ) as url:
            url.return_value = "https://blob.vercel-storage.com/x.png"
            self.assertEqual(
                respuestas.image_url(row), "https://blob.vercel-storage.com/x.png"
            )
        row.image.delete(save=False)

    def test_a_reply_without_an_image_has_no_url(self):
        self.assertEqual(respuestas.image_url(reply(title="Solo texto")), "")
