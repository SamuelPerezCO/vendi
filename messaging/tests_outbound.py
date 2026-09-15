"""Provider-level tests for what actually goes out on the wire: the image
payloads behind a quick reply with a picture, and the plain-text fallback a
provider without a template mechanism has to make."""

from unittest import mock

from django.test import TestCase, override_settings

from messaging.providers.base import MessagingProvider
from messaging.providers.fake import FakeProvider
from messaging.providers.meta import MetaProvider


class MetaSendImageTests(TestCase):
    @override_settings(
        META_ACCESS_TOKEN="tok", META_PHONE_NUMBER_ID="123", MESSAGING_PROVIDER="meta"
    )
    def test_the_payload_is_an_image_by_link_with_the_caption(self):
        provider = MetaProvider()
        with mock.patch.object(provider, "_post_message", return_value="wamid-1") as post:
            self.assertEqual(
                provider.send_image("+573167687288", "https://cdn.example/p.png", "Precios"),
                "wamid-1",
            )
        payload = post.call_args.args[0]
        self.assertEqual(payload["type"], "image")
        self.assertEqual(payload["to"], "+573167687288")
        self.assertEqual(
            payload["image"], {"link": "https://cdn.example/p.png", "caption": "Precios"}
        )

    @override_settings(META_ACCESS_TOKEN="tok", META_PHONE_NUMBER_ID="123")
    def test_an_empty_caption_is_omitted_rather_than_sent_blank(self):
        provider = MetaProvider()
        with mock.patch.object(provider, "_post_message", return_value="wamid-2") as post:
            provider.send_image("+57", "https://cdn.example/p.png")
        self.assertEqual(post.call_args.args[0]["image"], {"link": "https://cdn.example/p.png"})

    @override_settings(META_ACCESS_TOKEN="tok", META_PHONE_NUMBER_ID="123")
    def test_the_rendered_body_never_leaks_into_the_template_parameters(self):
        # Meta renders from its own approved copy; _rendered is for
        # text-only providers and must not become a body parameter.
        provider = MetaProvider()
        with mock.patch.object(provider, "_post_message", return_value="wamid-3") as post:
            provider.send_template(
                "+57", "saludo", {"1": "Camila", "_language": "es", "_rendered": "Hola Camila"}
            )
        payload = post.call_args.args[0]
        self.assertEqual(payload["template"]["language"], {"code": "es"})
        parameters = payload["template"]["components"][0]["parameters"]
        self.assertEqual(parameters, [{"type": "text", "text": "Camila"}])
        # No _category, so no extra components either.
        self.assertEqual(len(payload["template"]["components"]), 1)

    def test_an_authentication_send_puts_the_code_on_the_button_too(self):
        """Meta refuses an authentication send that fills the body and leaves
        the copy-code button empty. Same code, said twice."""
        provider = MetaProvider()
        with mock.patch.object(provider, "_post_message", return_value="wamid-4") as post:
            provider.send_template(
                "+57",
                "codigo_verificacion",
                {"1": "123456", "_language": "es", "_category": "authentication"},
            )
        components = post.call_args.args[0]["template"]["components"]
        self.assertEqual(
            components[1],
            {
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": "123456"}],
            },
        )

    def test_a_marketing_send_gets_no_button_component(self):
        provider = MetaProvider()
        with mock.patch.object(provider, "_post_message", return_value="wamid-5") as post:
            provider.send_template(
                "+57", "saludo", {"1": "Camila", "_category": "marketing"}
            )
        self.assertEqual(len(post.call_args.args[0]["template"]["components"]), 1)


class FakeProviderImageTests(TestCase):
    def test_send_image_returns_an_id_like_the_other_sends(self):
        message_id = FakeProvider().send_image("+57", "https://cdn.example/p.png", "hola")
        self.assertTrue(message_id.startswith("fake-"))


class BaseFallbackTests(TestCase):
    def test_a_provider_without_send_image_delivers_the_caption(self):
        class TextOnly(MessagingProvider):
            name = "textonly"
            def __init__(self):
                self.sent = []
            def send_text(self, to, body):
                self.sent.append((to, body)); return "t-1"
            def send_template(self, to, template_name, params): return "t-2"
            def parse_webhook(self, request): return []
            def verify_signature(self, request): return True

        provider = TextOnly()
        self.assertEqual(provider.send_image("+57", "https://x/y.png", "hola"), "t-1")
        self.assertEqual(provider.sent, [("+57", "hola")])
        provider.send_image("+57", "https://x/y.png")
        self.assertEqual(provider.sent[-1], ("+57", "https://x/y.png"))
