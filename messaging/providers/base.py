"""The provider interface every integration implements.

The contract is shaped so the real target fits without changes:

* **Meta Cloud API** -- JSON webhooks, ``X-Hub-Signature-256`` (HMAC-SHA256
  over the raw body), Bearer-token Graph API, a ``phone_number_id`` per line,
  and a one-off GET verification handshake (``hub.challenge``).

Hence the choices below:

* ``parse_webhook``/``verify_signature`` take the raw Django ``request``, not
  a parsed dict -- Meta needs the *raw bytes* for its HMAC plus the headers,
  and a form-encoded provider would need ``request.POST``.
* ``send_*`` take bare E.164 numbers; any addressing scheme (``whatsapp:``)
  is the provider's private business.
* ``handshake`` exists because Meta verifies the endpoint with a GET before
  it ever POSTs; providers that don't do that inherit the no-op.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .types import InboundEvent, TemplateSpec, TemplateVerdict


class MessagingProvider(ABC):
    """One messaging backend (Meta Cloud API, fake...)."""

    #: Registry key and URL slug: ``/webhooks/messaging/<name>/``.
    name: str = ""

    @abstractmethod
    def send_text(self, to: str, body: str) -> str:
        """Send a free-form text to ``to`` (E.164). Returns the provider's
        message id, which later status webhooks will reference.

        Only valid inside the 24-hour customer-service window -- callers
        enforce that (see ``services.send_message``); the provider just sends.

        Any exception means the platform did not take the message -- except
        ``types.SendOutcomeUnknown``, which a provider raises when the
        request went out and no answer came back, so the message may have
        been sent after all. Same for ``send_template`` and ``send_image``.
        """

    @abstractmethod
    def send_template(self, to: str, template_name: str, params: dict) -> str:
        """Send a pre-approved template message. The only way to reach someone
        outside the 24-hour window. Returns the provider's message id.

        ``params`` fills the template's placeholders. Three reserved keys
        ride in it. ``_language`` (the language code) and ``_rendered`` (the
        body with its sample values already substituted -- what the CRM shows
        in the thread) exist for providers with no template mechanism of
        their own, which must fall back to plain text: a provider with real
        template support ignores ``_rendered``, one without it sends exactly
        that string, so the customer reads the message rather than its name.
        ``_category`` is the plantilla's category, which only matters for
        ``authentication`` -- those are sent with an extra button component.

        Every reserved key starts with an underscore, so a provider that
        knows none of them can drop them all by that one rule and be left
        with the placeholder values.
        """

    def send_image(self, to: str, image_url: str, caption: str = "") -> str:
        """Send an image by public URL with an optional caption. Same window
        rule as ``send_text``. Returns the provider's message id.

        Not abstract: a provider that cannot ship media falls back to the
        caption as a text (the customer still gets the words), so a quick
        reply with a picture never fails outright on a text-only backend.
        """
        return self.send_text(to, caption or image_url)

    @abstractmethod
    def parse_webhook(self, request) -> list[InboundEvent]:
        """Normalize one webhook request into events. Called only after
        ``verify_signature`` has passed. Raise ``ValueError`` on a payload
        that cannot be understood -- the endpoint logs it and still answers
        200 so the provider does not enter a retry storm."""

    @abstractmethod
    def verify_signature(self, request) -> bool:
        """Whether the webhook request genuinely came from the provider.

        Checked *before* the body is trusted in any way; a ``False`` is
        answered with 401 and no processing. Each provider brings its own
        scheme (Meta: HMAC-SHA256 over the raw body)."""

    def handshake(self, request) -> str | None:
        """Answer a GET verification challenge, or ``None`` if the provider
        has no such thing. Meta sends ``hub.mode=subscribe`` with a
        ``hub.challenge`` to echo; other providers never GET the webhook."""
        return None

    # --- Template catalogue (optional) ---------------------------------------
    #
    # Only the official Cloud API keeps a catalogue of templates that must be
    # submitted and approved before ``send_template`` will accept them. The
    # defaults below are the "no catalogue" answer, so the fake provider
    # inherits them untouched --
    # same stance as ``handshake``.

    def create_template(self, spec: TemplateSpec) -> str | None:
        """Submit ``spec`` for approval. Returns the provider's id for the new
        template, or ``None`` when this provider has no catalogue to submit
        to (the CRM then simply keeps its own record). Raise on a rejected or
        failed submission -- the caller reports it, never guesses."""
        return None

    def template_verdicts(self) -> list[TemplateVerdict]:
        """Every template in the provider's catalogue with its current
        approval state, normalized. Empty when there is no catalogue."""
        return []
