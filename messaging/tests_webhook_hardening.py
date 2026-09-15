"""The webhook endpoint's fail-closed rules.

The hole these cover let anyone on the internet write rows into the
production database that the app then renders as real customers: the fake
simulator stayed routable on a deployment running a real provider, and its
shared secret shipped with a value committed to the repository. The
simulator is gone; these pin that its URL stays dead and that only providers
the app really has answer at all.

The database is shared with an external automation, so an injected row is
indistinguishable from a real conversation once it lands.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models import Client
from messaging.models import Conversation, Message
from messaging.providers import registry
from messaging.testing import StubProvider

#: What the fake simulator's webhook used to turn straight into a customer.
INBOUND = {
    "events": [
        {
            "event_type": "message",
            "provider_message_id": "forged-1",
            "from_number": "+573001112233",
            "to_number": "+573000000000",
            "body": "inyectado",
            "contact_name": "Inyectado",
        }
    ]
}


def url(provider: str) -> str:
    return reverse("messaging_webhook", args=[provider])


def post(client, provider: str):
    return client.post(
        url(provider),
        data=json.dumps(INBOUND),
        content_type="application/json",
        headers={"X-Fake-Signature": "dev-secret"},
    )


class RemovedFakeProviderTests(TestCase):
    """The fake provider minted contacts and messages straight out of the
    request body. Its URL must stay a 404, with nothing written."""

    def test_the_fake_webhook_is_a_404(self):
        response = post(self.client, "fake")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    def test_its_handshake_is_gone_too(self):
        # Otherwise it would reflect hub.challenge unauthenticated on the
        # production origin.
        response = self.client.get(url("fake"), {"hub.challenge": "<script>x</script>"})

        self.assertEqual(response.status_code, 404)

    def test_it_is_not_a_known_provider(self):
        self.assertFalse(registry.is_known_provider("fake"))


class StubWebhookTests(TestCase):
    """The test-only stub is a known provider while tests run, so its URL
    resolves -- and must still refuse everything."""

    def test_a_request_to_the_stub_is_refused(self):
        response = post(self.client, "stub")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Message.objects.count(), 0)


class ConfiguredProviderTests(TestCase):
    """MESSAGING_PROVIDER must name a provider that exists.

    An unknown value used to pass startup and raise only when something tried
    to send, so a deployment looked healthy while every outbound message
    crashed. That matters most right after a provider is dropped, when an
    environment can still be carrying its name.
    """

    def test_the_settings_list_matches_the_registry(self):
        """Two places name the providers; drift between them is the bug this
        catches (settings cannot import the registry at settings time). The
        test-only stub is registered under TESTING and is not one of them."""
        known = sorted(name for name in registry._PROVIDERS if name != StubProvider.name)
        self.assertEqual(sorted(settings.MESSAGING_PROVIDERS), known)

    def test_a_provider_this_app_lacks_is_not_known(self):
        self.assertFalse(registry.is_known_provider("retirado"))

    def test_its_webhook_slug_is_a_404(self):
        response = self.client.post(
            "/webhooks/messaging/retirado/", data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
