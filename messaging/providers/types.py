"""Provider-agnostic value types.

Every provider's webhook payload -- Meta's nested JSON, the fake provider's
flat JSON -- is normalized into these before the rest
of the app sees it. Nothing outside ``providers/`` should ever touch a raw
provider payload.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


#: Body shown when a media message carries no text of its own -- an image
#: with no caption should still read as something, not as an empty bubble.
#: Shared here (not per-provider) because the Inbox also needs to know these
#: are placeholders: once the image itself renders in the thread, repeating
#: "[sticker]" under it as text is noise (see ``Message.display_body``).
MEDIA_PLACEHOLDERS = {
    "image": "[imagen]",
    "video": "[video]",
    "audio": "[audio]",
    "document": "[documento]",
    "sticker": "[sticker]",
}


#: Label of an authentication template's copy-code button when the editor
#: leaves it blank. Shared by ``core.plantillas`` (the field's placeholder and
#: default) and the Meta provider (what it actually submits), so the button a
#: person sees previewed is the button that goes for approval.
AUTH_BUTTON_TEXT_DEFAULT = "Copiar código"

#: How long an authentication code may be said to last, in minutes -- the
#: range the platform accepts.
AUTH_EXPIRATION_MIN = 1
AUTH_EXPIRATION_MAX = 90
AUTH_EXPIRATION_DEFAULT = 10


class SendOutcomeUnknown(Exception):
    """A send whose result the provider never reported.

    Raised by a provider when the request reached the platform but the
    answer did not come back -- a read timeout, typically. The message may
    well be on its way (Meta accepts a send in well under a second and can
    still take much longer to answer), so the caller must not call it
    failed: ``services.send_message`` leaves the row ``queued`` and the
    delivery receipt, when it arrives, claims the row by recipient (see
    ``services._adopt_unconfirmed_send``).

    Distinct from every other error out of a send, which all mean the
    platform did *not* take the message: those stay plain exceptions.
    """


class MessageStatus(str, enum.Enum):
    """Delivery lifecycle of an outbound message.

    Values are stored verbatim in ``Message.status``. The order here is the
    canonical progression; :func:`status_rank` turns it into a comparison so
    out-of-order webhook deliveries (provider retries, Meta batching) can never
    move a message *backwards* -- e.g. a late "delivered" after "read".
    """

    QUEUED = "queued"        # created locally, not yet accepted by the provider
    SENT = "sent"            # accepted by the provider
    DELIVERED = "delivered"  # reached the recipient's device
    READ = "read"            # recipient opened it
    FAILED = "failed"        # provider gave up (terminal)


_STATUS_ORDER = [
    MessageStatus.QUEUED,
    MessageStatus.SENT,
    MessageStatus.DELIVERED,
    MessageStatus.READ,
]


def status_rank(status: str) -> int:
    """Position of ``status`` in the delivery progression.

    ``failed`` ranks highest: it is terminal, and a retry storm re-delivering
    an old "sent" must not resurrect a message the provider already gave up on.
    Unknown values rank lowest so they can never overwrite anything.
    """
    try:
        return _STATUS_ORDER.index(MessageStatus(status))
    except ValueError:
        return -1 if status != MessageStatus.FAILED.value else len(_STATUS_ORDER)


class TemplateStatus(str, enum.Enum):
    """Approval state of a message template, in the CRM's vocabulary.

    Values are stored verbatim in ``MessageTemplate.status``. Providers
    translate their own words into these (Meta says APPROVED/PENDING/
    REJECTED, plus states like PAUSED and DISABLED that mean "you cannot
    send this") so nothing outside ``providers/`` learns a second dialect --
    the same normalization :class:`MessageStatus` does for deliveries.
    """

    PENDING = "pendiente"
    APPROVED = "aceptada"
    REJECTED = "rechazada"


@dataclass(frozen=True)
class TemplateSpec:
    """One message template on its way *out* to a provider for approval.

    The mirror image of :class:`InboundEvent`: a normalized shape so
    ``providers/`` never imports a Django model and the CRM never builds a
    provider's payload. ``core.plantillas.template_spec`` is the one place a
    ``MessageTemplate`` becomes one of these.
    """

    name: str
    language: str
    category: str
    """``marketing`` | ``utility`` | ``authentication`` (MessageTemplate.CATEGORY_CHOICES)."""

    body: str
    body_sample_values: list[str] = field(default_factory=list)
    """One example per {{n}}, in numeric order. Meta rejects a template whose
    body has variables and no examples."""

    header_type: str = "none"
    """``none`` | ``text`` | ``image`` | ``video`` | ``document``."""

    header_text: str = ""

    header_media: object | None = None
    """The uploaded example file for a media header, as an open file-like
    object (a Django ``FieldFile``). Meta will not accept a media header
    without a sample of the real bytes, so a URL is not enough."""

    footer: str = ""

    buttons: list[dict] = field(default_factory=list)
    """``MessageTemplate.buttons`` verbatim: ``{"type": "quick_reply"|"url"|
    "phone", "text": ..., ...}`` dicts."""

    # --- authentication only ------------------------------------------------
    #
    # An authentication template carries none of the copy above: the platform
    # writes and localizes the wording itself and accepts only these three
    # settings. They are ignored for every other category, and every field
    # above is ignored for this one.

    auth_security_recommendation: bool = True
    """Append the platform's own "no compartas este código" line."""

    auth_code_expiration_minutes: int | None = None
    """Show a "el código vence en N minutos" footer. None omits the footer."""

    auth_button_text: str = ""
    """Label of the copy-code button. Empty falls back to
    :data:`AUTH_BUTTON_TEXT_DEFAULT`."""


@dataclass(frozen=True)
class TemplateVerdict:
    """What a provider currently says about one template it holds.

    ``(name, language)`` is the key, matching the CRM's own uniqueness rule --
    Meta scopes template names per language too.
    """

    name: str
    language: str
    status: TemplateStatus
    rejection_reason: str = ""
    provider_template_id: str = ""


@dataclass(frozen=True)
class InboundEvent:
    """One normalized event out of a webhook payload.

    A single webhook request can carry many of these (Meta batches, the fake
    provider's status ticks), which is why ``parse_webhook`` returns a list.

    Two shapes share the class, discriminated by ``event_type``:

    * ``"message"`` -- someone wrote to us. ``body``/``media_url`` matter,
      ``status`` is ignored.
    * ``"status"``  -- a delivery receipt for a message *we* sent.
      ``status`` matters, ``body`` is ignored.
    """

    event_type: str
    """``"message"`` or ``"status"``."""

    provider_message_id: str
    """The provider's id for the message. This is the idempotency key: the
    same id arriving twice (providers retry aggressively) must not create a
    second row."""

    from_number: str = ""
    """Sender in E.164 (``+57316...``). Providers that prefix an address
    scheme (such as ``whatsapp:+57...``) strip it in ``parse_webhook``."""

    to_number: str = ""
    """Our number, E.164. Unused while the app has a single inbox; kept so
    multi-number routing needs no interface change."""

    body: str = ""

    media_url: str = ""

    media_type: str = ""
    """What kind of attachment ``media_url`` points at, using the provider's
    coarse vocabulary (a ``MEDIA_PLACEHOLDERS`` key: ``image``, ``video``,
    ``audio``, ``document``, ``sticker``). Empty for text-only messages. The
    Inbox uses it to render images/stickers inline instead of as a download
    link."""

    timestamp: datetime | None = None
    """Provider-reported time, timezone-aware. ``None`` -> receipt time."""

    status: MessageStatus | None = None
    """For ``status`` events: the new delivery state."""

    channel: str = "whatsapp"
    """Conversation channel key (see ``Conversation.CHANNEL_CHOICES``). Meta's
    webhook also carries Messenger/Instagram traffic, so the event says which."""

    contact_name: str = field(default="")
    """Display name if the provider shares it (Meta's ``profile.name``). Used
    only when the phone number is new to the CRM."""

    pricing: dict | None = None
    """For ``status`` events from a provider that reports billing: what the
    platform says this message cost it.

    Meta puts a ``pricing`` object on the ``sent`` status and on one of
    ``delivered``/``read``. It carries no money -- only which *rate bucket*
    applies -- so the CRM keeps its own amount and uses this to correct it
    (``services._apply_status_event``). Normalized keys, all optional because
    Meta's payloads are not consistent about them:

    * ``billable``  -- bool. Being deprecated in favour of ``type``.
    * ``model``     -- ``"PMP"`` (per-message, the default since 2025-07-01)
      or ``"CBP"`` (the legacy conversation model).
    * ``category``  -- the rate Meta actually applied: ``marketing``,
      ``utility``, ``authentication``, ``authentication-international``,
      ``service``, ``marketing_lite``, ``referral_conversion``. This is
      Meta's verdict, which can differ from the category stored on the
      plantilla -- Meta re-categorises templates.
    * ``type``      -- ``regular`` (billable), ``free_customer_service`` or
      ``free_entry_point``.

    ``None`` when the provider says nothing about billing, which is every
    provider but Meta today.
    """
