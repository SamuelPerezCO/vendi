"""Webhook secrets fail closed.

Meta's webhook stays reachable by design -- a status callback must still
parse with the provider that sent it -- so its shared secret is the only
thing in front of the database.

Which makes a secret with a default a secret an attacker already has, and
this repository used to ship one. Now an empty secret rejects everything
rather than accepting a published default, and the subscribe handshake never
reflects the caller's input as HTML.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Client
from messaging.models import Message

#: One inbound text in Meta's nested webhook shape.
INBOUND = json.dumps({
    "object": "whatsapp_business_account",
    "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15551957906", "phone_number_id": "123"},
        "contacts": [{"wa_id": "573001112233", "profile": {"name": "Inyectado"}}],
        "messages": [{"id": "wamid.FORGED", "from": "573001112233", "type": "text",
                      "text": {"body": "inyectado"}}],
    }}]}],
})


def signed_with(secret: str) -> dict:
    digest = hmac.new(
        secret.encode("utf-8"), INBOUND.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def post(client, headers):
    return client.post(
        reverse("messaging_webhook", args=["meta"]),
        data=INBOUND,
        content_type="application/json",
        headers=headers,
    )


class MetaSecretTests(TestCase):
    @override_settings(META_APP_SECRET="")
    def test_an_unset_secret_rejects_instead_of_waving_traffic_through(self):
        # Signed with the empty key: exactly what a forger can compute.
        response = post(self.client, signed_with(""))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    @override_settings(META_APP_SECRET="app-secret")
    def test_a_configured_secret_lets_meta_in(self):
        self.assertEqual(post(self.client, signed_with("app-secret")).status_code, 200)
        self.assertEqual(Message.objects.count(), 1)


@override_settings(META_VERIFY_TOKEN="verify-token")
class HandshakeTests(TestCase):
    """The challenge is whatever the caller sent, so it must never come back
    as HTML on the deployment's own origin."""

    def test_the_challenge_is_returned_as_plain_text(self):
        response = self.client.get(
            reverse("messaging_webhook", args=["meta"]),
            {"hub.mode": "subscribe", "hub.verify_token": "verify-token",
             "hub.challenge": "abc123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"abc123")
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
