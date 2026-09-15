"""Provider-level tests for what actually goes out on the wire: the image
payloads behind a quick reply with a picture, and the caption fallback a
provider without media support inherits from the base class."""

from unittest import mock

from django.test import TestCase, override_settings

from messaging.providers.base import MessagingProvider
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
            def create_template(self, spec): return "tpl-1"
            def template_verdicts(self): return []

        provider = TextOnly()
        self.assertEqual(provider.send_image("+57", "https://x/y.png", "hola"), "t-1")
        self.assertEqual(provider.sent, [("+57", "hola")])
        provider.send_image("+57", "https://x/y.png")
        self.assertEqual(provider.sent[-1], ("+57", "https://x/y.png"))
