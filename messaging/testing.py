"""The messaging provider ``manage.py test`` sends through.

Test code only. :class:`messaging.apps.MessagingConfig` registers
:class:`StubProvider` when ``settings.TESTING`` is true and at no other time,
and settings select it only then, so it cannot be chosen in development or on
a deployment -- those run Meta, the only provider the app has. Tests must
never depend on a live Meta connection to pass.

The stub is deliberately inert: sends succeed with a ``stub-`` id and reach
no one, there is no webhook (its signature check refuses every request) and
there are no delivery receipts. Inbound tests hand
:class:`~messaging.providers.types.InboundEvent` objects straight to
``services.process_inbound_events``, or sign a payload for Meta's own
endpoint. A test that needs a send to fail patches the method.
"""

from __future__ import annotations

import uuid

from .providers import registry
from .providers.base import MessagingProvider
from .providers.types import InboundEvent, TemplateSpec, TemplateVerdict


class StubProvider(MessagingProvider):
    name = "stub"

    def send_text(self, to: str, body: str) -> str:
        return _message_id()

    def send_template(self, to: str, template_name: str, params: dict) -> str:
        return _message_id()

    def verify_signature(self, request) -> bool:
        return False  # no webhook: every request is refused with 401

    def parse_webhook(self, request) -> list[InboundEvent]:
        raise ValueError("the stub provider has no webhook")

    def create_template(self, spec: TemplateSpec) -> str:
        return f"stub-tpl-{uuid.uuid4().hex}"

    def template_verdicts(self) -> list[TemplateVerdict]:
        return []


def _message_id() -> str:
    return f"stub-{uuid.uuid4().hex}"


def register() -> None:
    """Make ``"stub"`` a provider the registry knows. Called once, from
    ``MessagingConfig.ready``, and only under TESTING."""
    registry._PROVIDERS[StubProvider.name] = StubProvider
