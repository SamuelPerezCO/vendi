"""Application services: everything between the UI/webhook and the provider.

Two entry points matter:

* :func:`process_inbound_events` -- the "heavy" half of webhook handling,
  kept out of the view so it can move behind a task queue (Celery/RQ) later
  without touching the endpoint: the view would enqueue the normalized events
  instead of calling this directly. There is no queue today (requirements.txt
  is Django only), so it runs synchronously.
* :func:`send_message` -- the only way the app sends a free-form text (or
  a quick reply's image). It is where the 24-hour window rule lives.
* :func:`send_template` -- the only way out of a closed window: a
  pre-approved plantilla with its {{n}} filled in, sent through the
  provider's template call. It is also how a conversation is started from
  our side (see :func:`start_conversation`).
* :func:`submit_template` / :func:`sync_template_verdicts` -- the plantilla
  catalogue's round trip to the provider: submit for approval, read the
  verdicts back. No-ops on providers without a catalogue.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Client

from . import pricing
from .models import Conversation, ConversationTag, Message, Tag
from .providers.registry import get_provider
from .providers.types import (
    InboundEvent,
    MessageStatus,
    SendOutcomeUnknown,
    TemplateStatus,
    status_rank,
)

logger = logging.getLogger(__name__)


class SendWindowClosed(Exception):
    """Raised when a free-form send is attempted outside the 24-hour window.

    A WhatsApp platform rule: after 24h of customer silence only pre-approved
    templates may be sent. Enforced here so it fails loudly in development
    instead of surfacing as provider errors in production.
    """


class SendFailed(Exception):
    """The provider rejected or errored on a send. The Message row is kept
    with ``status=failed`` so the thread shows what happened."""


class SendUnconfirmed(SendFailed):
    """The provider took the request but never said what it did with it.

    A subclass of :class:`SendFailed` so existing ``except SendFailed`` sites
    keep working, but the row is left ``queued`` rather than ``failed``: the
    message has most likely gone out (Meta answers slowly far more often than
    it drops a request), and a red tick plus "inténtalo de nuevo" would push
    the agent into sending the customer a duplicate. The delivery receipt
    settles it -- ``_apply_status_event`` claims the id-less row by recipient
    when the receipt arrives (:func:`_adopt_unconfirmed_send`).
    """


class TemplateNotSendable(Exception):
    """Raised when a plantilla cannot go out as a template message: it is
    switched off, or Meta rejected it. A *pendiente* one is allowed through --
    the MVP has no approval pipeline of its own, and the provider is the
    authority on whether an unapproved name sends (the fake provider always
    will, Meta will answer with an error that surfaces as SendFailed)."""


class TemplateSubmissionFailed(Exception):
    """The provider refused or errored on a template submission. The
    plantilla row is kept, unsubmitted, so the editor's work is not lost --
    the message says what Meta objected to."""


class BudgetExceeded(Exception):
    """The send would push this month past ``MESSAGING_MONTHLY_BUDGET``.

    Template sends cost real money, so the ceiling is enforced here rather
    than trusted to the UI -- a bulk loop and a hand-crafted POST hit the
    same guard the dialog does.

    Raised by :func:`send_template` after pricing and before the Message row
    is written, so a refused send leaves no trace in the thread.
    The view driving the send catches it and shows ``str(exc)`` (a
    Spanish sentence naming the ceiling) as the dialog's error line.
    """


# --- Sending ---------------------------------------------------------------


def send_message(
    conversation: Conversation,
    body: str,
    user=None,
    *,
    image_url: str = "",
) -> Message:
    """Send a free-form message in ``conversation`` and record it.

    Text by default; with ``image_url`` (a public URL -- a quick reply's
    stored picture) the provider ships the image and ``body`` rides along as
    its caption. Either way it is a free-form send, bound by the same 24h
    window: media is no more allowed outside it than text is.

    The Message row is created *before* the provider call (status ``queued``)
    so a crash mid-send leaves evidence rather than losing the message; the
    provider's id is attached once the call returns.

    Raises :class:`SendWindowClosed` outside the 24h window and
    :class:`SendFailed` when the provider errors (row kept as ``failed``).
    """
    if not conversation.is_within_24h_window:
        raise SendWindowClosed(
            "La ventana de 24 horas está cerrada: WhatsApp solo permite enviar "
            "una plantilla aprobada hasta que el cliente vuelva a escribir. "
            "(Free-form sends outside the 24h customer-service window are "
            "rejected by the platform -- use send_template.)"
        )

    message = Message.objects.create(
        conversation=conversation,
        direction=Message.OUTBOUND,
        body=body,
        media_url=image_url,
        media_type="image" if image_url else "",
        status=MessageStatus.QUEUED.value,
        sent_by=user if getattr(user, "is_authenticated", False) else None,
    )

    provider = get_provider()
    try:
        if image_url:
            provider_id = provider.send_image(
                to=conversation.contact.phone, image_url=image_url, caption=body
            )
        else:
            provider_id = provider.send_text(to=conversation.contact.phone, body=body)
    except SendOutcomeUnknown as exc:
        # Reached the platform, no answer: the row stays queued (clock icon)
        # for the receipt to claim. It is still the latest thing in the thread.
        conversation.last_message_at = message.timestamp
        conversation.save(update_fields=["last_message_at"])
        logger.warning("send unconfirmed for conversation %s: %s", conversation.pk, exc)
        raise SendUnconfirmed(str(exc)) from exc
    except Exception as exc:
        message.status = MessageStatus.FAILED.value
        message.save(update_fields=["status"])
        logger.exception("send_text failed for conversation %s", conversation.pk)
        raise SendFailed(str(exc)) from exc

    message.provider_message_id = provider_id
    message.save(update_fields=["provider_message_id"])

    conversation.last_message_at = message.timestamp
    conversation.save(update_fields=["last_message_at"])
    return message


def send_template(conversation: Conversation, template, values: dict, user=None) -> Message:
    """Send plantilla ``template`` in ``conversation`` with its {{n}} filled
    from ``values`` (``{"1": "Ana", "2": "#4512"}``), and record it.

    This is the one send that ignores the 24-hour window -- that is the
    entire reason template messages exist, and it is how a conversation is
    started from our side (a client who has never written in) or restarted
    (one who went quiet). The Message row stores the *rendered* text
    (core.plantillas.render_with), so the thread reads as what the customer
    received rather than as ``order_confirmation``.

    ``values`` comes from the agent filling the send dialog, never from the
    plantilla's editor samples: those are examples for Meta's reviewer, and
    sending them would greet every customer as "Camila".

    Same crash-safety shape as :func:`send_message`: the row is created
    ``queued`` before the provider call and marked ``failed`` if it errors.
    Raises :class:`TemplateNotSendable` for an inactive or rejected plantilla
    and :class:`SendFailed` when the provider errors.
    """
    from core import plantillas  # local: core imports this module

    if not template.is_active:
        raise TemplateNotSendable("La plantilla está desactivada.")
    if template.status == TemplateStatus.REJECTED.value:
        raise TemplateNotSendable("WhatsApp rechazó esta plantilla; no se puede enviar.")

    body = plantillas.render_with(template, values)

    # Price it before writing anything. The quote depends on the plantilla's
    # category, the client's country and whether this thread's window is open
    # -- a utility template inside the window is billed as a service message.
    # A brand-new thread has no inbound message, so its window reads closed
    # and the full rate applies.
    quote = pricing.quote(
        template,
        conversation.contact,
        window_open=conversation.is_within_24h_window,
        # How much of this month's free service allowance is already spent.
        # Passed in rather than looked up inside quote(), which is otherwise
        # pure arithmetic over settings and two model instances.
        service_used=pricing.service_used_this_month(),
    )
    # The monthly ceiling is enforced here rather than in the dialog, so a
    # bulk loop and a hand-crafted POST meet the same guard. Nothing locks
    # between this check and the insert, so two sends racing can both pass.
    if pricing.would_exceed_budget(quote.amount):
        raise BudgetExceeded(
            "Este envío supera el presupuesto mensual de plantillas "
            f"({pricing.budget()} {quote.currency}). Súbelo o espera al "
            "próximo mes."
        )

    # The three billed_* columns are copied from the quote here and never
    # recomputed -- that is what "frozen" means. Meta's own verdict arrives
    # later on the delivery receipt and corrects them (_apply_pricing).
    message = Message.objects.create(
        conversation=conversation,
        direction=Message.OUTBOUND,
        body=body,
        status=MessageStatus.QUEUED.value,
        sent_by=user if getattr(user, "is_authenticated", False) else None,
        template=template,
        billed_category=quote.category,
        billed_amount=quote.amount,
        billed_currency=quote.currency,
        billed_as_service=quote.billed_as_service,
    )

    provider = get_provider()
    params = {str(key): str(value) for key, value in values.items()}
    params["_language"] = template.language
    # Authentication templates need the code on their copy-code button as
    # well as in the body; only the provider knows that shape.
    params["_category"] = template.category
    # For providers with no template catalogue, so they send the
    # message rather than the template's name -- see MessagingProvider.
    params["_rendered"] = body
    try:
        provider_id = provider.send_template(
            to=conversation.contact.phone, template_name=template.name, params=params
        )
    except SendOutcomeUnknown as exc:
        # As in send_message: queued, not failed. The quote stays on the row
        # because the send probably went out; Meta's receipt corrects it as
        # it corrects every other template send (_apply_pricing).
        conversation.last_message_at = message.timestamp
        conversation.save(update_fields=["last_message_at"])
        logger.warning(
            "send_template %s unconfirmed for conversation %s: %s",
            template.name, conversation.pk, exc,
        )
        raise SendUnconfirmed(str(exc)) from exc
    except Exception as exc:
        message.status = MessageStatus.FAILED.value
        # A send the provider refused was never delivered, so it is never
        # billed -- zero rather than NULL, which would read as "not priced".
        message.billed_amount = Decimal("0")
        message.save(update_fields=["status", "billed_amount"])
        logger.exception(
            "send_template %s failed for conversation %s", template.name, conversation.pk
        )
        raise SendFailed(str(exc)) from exc

    message.provider_message_id = provider_id
    message.save(update_fields=["provider_message_id"])

    conversation.last_message_at = message.timestamp
    conversation.save(update_fields=["last_message_at"])
    return message


def start_conversation(contact: Client, channel: str = "whatsapp") -> Conversation:
    """The thread to send a first message in: the contact's open one on that
    channel, or a fresh row. Same rule as inbound routing, so an agent
    starting a chat and a customer writing in land in the same place."""
    return _get_or_create_open_conversation(contact, channel)


# --- Template catalogue ------------------------------------------------------


def submit_template(template) -> bool:
    """Submit a freshly saved plantilla to the provider for approval.

    Returns True when it was submitted (``provider_template_id`` is set) and
    False when the active provider keeps no catalogue -- the plantilla then
    simply stays a local record, which is what every provider but Meta means.
    Raises :class:`TemplateSubmissionFailed` when the provider objects; the
    caller decides how to show that, the row is already saved either way.
    """
    from core import plantillas  # local: core imports this module

    provider = get_provider()
    try:
        provider_id = provider.create_template(plantillas.template_spec(template))
    except Exception as exc:
        logger.exception("create_template %s failed", template.name)
        raise TemplateSubmissionFailed(str(exc)) from exc

    if provider_id is None:
        return False
    template.provider_template_id = provider_id
    template.status = TemplateStatus.PENDING.value
    template.save(update_fields=["provider_template_id", "status"])
    return True


def sync_template_verdicts() -> int:
    """Read every approval verdict the provider has and write the changed
    ones onto the matching plantillas. Returns how many rows changed.

    Matched by (name, language) -- the CRM's own uniqueness key and Meta's --
    so a template created straight in Meta's console still reconciles with a
    plantilla of the same name here, and one whose submission never recorded
    an id catches up. Plantillas the provider does not know are left alone:
    absence is not a verdict (the catalogue may have been created after them,
    or the sync may be talking to a different WABA than the one they went to).

    Every matched row gets ``status_synced_at`` stamped even when nothing
    else moved, so the page can say *when* "Pendiente" was last true.
    """
    from core.models import MessageTemplate  # local: core imports this module

    verdicts = get_provider().template_verdicts()
    if not verdicts:
        return 0

    by_key = {(verdict.name, verdict.language): verdict for verdict in verdicts}
    now = timezone.now()
    changed = 0
    for template in MessageTemplate.objects.filter(
        name__in={verdict.name for verdict in verdicts}
    ):
        verdict = by_key.get((template.name, template.language))
        if verdict is None:
            continue
        fields = ["status_synced_at"]
        template.status_synced_at = now
        if template.status != verdict.status.value:
            template.status = verdict.status.value
            fields.append("status")
        if template.rejection_reason != verdict.rejection_reason:
            template.rejection_reason = verdict.rejection_reason
            fields.append("rejection_reason")
        if verdict.provider_template_id and (
            template.provider_template_id != verdict.provider_template_id
        ):
            template.provider_template_id = verdict.provider_template_id
            fields.append("provider_template_id")
        template.save(update_fields=fields)
        if len(fields) > 1:
            changed += 1
    return changed


def conversation_for_client(client: Client, channel: str = "whatsapp") -> Conversation:
    """The thread a message to ``client`` belongs in, created if there is none.

    This is what makes writing to a *new* client possible at all: someone
    added by hand in the CRM (or imported from a list) has never written, so
    no conversation exists to open in the Inbox. Reuses the same
    open-or-create rule inbound messages follow, so a template send and the
    customer's eventual reply land in one thread rather than two.

    Called only once a send is really about
    to happen (the dialog itself uses :func:`find_open_conversation`, which
    never creates) and by the tests. ``channel`` defaults to WhatsApp because
    plantillas are a WhatsApp mechanism. Delegates to
    :func:`_get_or_create_open_conversation`: an open or pending thread on
    that channel is returned as is; otherwise a new ``Conversation`` row is
    inserted with the model defaults (status ``open``, no messages, no
    ``last_message_at`` yet).
    """
    return _get_or_create_open_conversation(client, channel)


def sendable_templates():
    """The plantillas the send dialog offers, best first.

    Same stance as the Inbox's Respuestas rápidas picker: every active
    plantilla WhatsApp has not rejected, pendientes included. A real account
    can only send *aceptada* ones, but nothing here approves a plantilla on
    its own, so filtering pendientes out would leave every freshly created
    one unusable. The UI badges them instead of hiding them.

    "rechazada" carries more than a rejection: :func:`sync_template_verdicts`
    stores Meta's PAUSED and DISABLED as rechazada too, because what matters
    to an agent is the same either way -- WhatsApp will refuse the send. So
    this one test covers both "Meta said no" and "Meta has suspended it",
    and a plantilla Meta has never seen keeps the lenient default.

    Returns a lazy QuerySet; the views wrap it in ``list()``. "aceptada"
    sorts before "pendiente", which is also the order to offer them in.
    """
    from core.models import MessageTemplate  # local: core imports this module

    return (
        MessageTemplate.objects.filter(is_active=True)
        .exclude(status=TemplateStatus.REJECTED.value)
        .order_by("status", "name")
    )


# --- Inbound processing -----------------------------------------------------


def process_inbound_events(events: list[InboundEvent]) -> None:
    """Apply normalized webhook events to the database.

    Each event is isolated: one bad event is logged and skipped, the rest
    still land -- the webhook has already answered 200 by contract, so there
    is no one left to report a partial failure to.
    """
    for event in events:
        try:
            with transaction.atomic():
                if event.event_type == "status":
                    _apply_status_event(event)
                elif event.event_type == "outbound_message":
                    _apply_outbound_event(event)
                else:
                    _apply_message_event(event)
        except Exception:
            logger.exception("failed to process inbound event %r", event)


def _apply_message_event(event: InboundEvent) -> None:
    """Someone wrote to us: upsert the Client, land the Message."""
    if not event.from_number:
        raise ValueError("message event without from_number")

    # Idempotency first: providers retry deliveries aggressively, and the
    # same provider_message_id must never become two rows. The unique
    # constraint on the column backs this up against races.
    if Message.objects.filter(provider_message_id=event.provider_message_id).exists():
        logger.info("skipping duplicate message %s", event.provider_message_id)
        return

    contact = _upsert_contact(event.from_number, event.contact_name, event.channel)
    conversation = _get_or_create_open_conversation(contact, event.channel)

    timestamp = event.timestamp or timezone.now()
    try:
        Message.objects.create(
            conversation=conversation,
            direction=Message.INBOUND,
            body=event.body,
            media_url=event.media_url,
            media_type=event.media_type,
            # Inbound rows are "delivered" by definition -- they reached us.
            status=MessageStatus.DELIVERED.value,
            provider_message_id=event.provider_message_id,
            timestamp=timestamp,
        )
    except IntegrityError:
        # A concurrent retry won the race between our exists() check and the
        # insert; the message is already stored, which is all that matters.
        logger.info("duplicate message %s lost insert race", event.provider_message_id)
        return

    # An inbound message always (re)opens the thread and the 24h window.
    conversation.last_message_at = timestamp
    conversation.last_inbound_at = timestamp
    conversation.unread_count += 1
    if conversation.status == Conversation.RESOLVED:
        conversation.status = Conversation.OPEN
    conversation.save(
        update_fields=["last_message_at", "last_inbound_at", "unread_count", "status"]
    )

    # The automations, last and each on its own: the message is already
    # stored, so a failure here costs the greeting or the assignment, never
    # the customer's message. Both are no-ops until switched on.
    try:
        auto_assign(conversation)
    except Exception:
        logger.exception("auto-assign failed for conversation %s", conversation.pk)
    try:
        send_welcome(conversation)
    except Exception:
        logger.exception("welcome failed for conversation %s", conversation.pk)


# --- Automations -------------------------------------------------------------
#
# Both run from the inbound path above and both start switched off. They are
# separate functions rather than inline branches so the Inbox's own paths --
# and the tests -- can call them directly.


def auto_assign(conversation: Conversation):
    """Give an unassigned conversation to the next agent in the rotation.

    Returns the user it went to, or ``None`` when the feature is off, the
    conversation already has an owner, or nobody is configured to receive it.

    The cursor advances inside a transaction with the settings row locked, so
    two conversations arriving at the same moment take different turns rather
    than both reading the same position. That lock is the whole reason this
    is not a stateless ``count % len(agents)``.
    """
    from core.agents import agent_users
    from core.models import MessagingSettings

    if conversation.assigned_to_id is not None:
        return None

    with transaction.atomic():
        settings_row = MessagingSettings.objects.select_for_update().filter(pk=1).first()
        if settings_row is None or not settings_row.assign_enabled:
            return None

        agents = agent_users()
        if not agents:
            # Switched on with nobody to assign to: leave it in the common
            # tray rather than inventing an owner.
            return None

        agent = agents[settings_row.assign_cursor % len(agents)]
        settings_row.assign_cursor = (settings_row.assign_cursor + 1) % len(agents)
        settings_row.save(update_fields=["assign_cursor", "updated_at"])

    conversation.assigned_to = agent
    conversation.save(update_fields=["assigned_to"])
    logger.info("auto-assigned conversation %s to %s", conversation.pk, agent.username)
    return agent


def send_welcome(conversation: Conversation):
    """Send the welcome message the first time this person ever writes.

    Returns the Message, or ``None`` when the feature is off, there is no
    text to send, or this contact has already been greeted.

    "Already greeted" is decided by a conditional UPDATE on
    ``Client.welcomed_at`` rather than by reading it first: the read-then-write
    version greets twice when two messages land together, which is exactly
    what a customer sending "hola" and their question in quick succession
    looks like. Whoever wins the UPDATE sends; everyone else returns.
    """
    from core.models import Client, MessagingSettings

    settings_row = MessagingSettings.objects.filter(pk=1).first()
    if settings_row is None or not settings_row.welcome_enabled:
        return None
    body = (settings_row.welcome_body or "").strip()
    if not body:
        return None

    claimed = Client.objects.filter(
        pk=conversation.contact_id, welcomed_at__isnull=True
    ).update(welcomed_at=timezone.now())
    if not claimed:
        return None

    try:
        return send_message(conversation, body)
    except (SendWindowClosed, SendFailed):
        # Hand the turn back: the greeting did not go out, so the next
        # message from this person should still get one.
        Client.objects.filter(pk=conversation.contact_id).update(welcomed_at=None)
        raise


def _apply_outbound_event(event: InboundEvent) -> None:
    """A message we sent through a channel other than this app's own Inbox --
    e.g. an agent replying straight from the paired WhatsApp phone instead of
    the CRM. Recorded so the thread reflects what was actually said either
    way, but unlike :func:`_apply_message_event` it does not touch
    ``last_inbound_at``/``unread_count``/``status``: those track the customer
    side of the 24h window and unread state, neither of which a message we
    sent affects (matching :func:`send_message`, the Inbox's own send path)."""
    if not event.to_number:
        raise ValueError("outbound event without to_number")

    if Message.objects.filter(provider_message_id=event.provider_message_id).exists():
        logger.info("skipping duplicate message %s", event.provider_message_id)
        return

    # No contact_name here: the provider's display name on one of *our* sends
    # is our own name, not the customer's -- useless for naming their Client.
    contact = _upsert_contact(event.to_number, "", event.channel)
    conversation = _get_or_create_open_conversation(contact, event.channel)

    timestamp = event.timestamp or timezone.now()
    try:
        Message.objects.create(
            conversation=conversation,
            direction=Message.OUTBOUND,
            body=event.body,
            media_url=event.media_url,
            media_type=event.media_type,
            status=MessageStatus.DELIVERED.value,
            provider_message_id=event.provider_message_id,
            timestamp=timestamp,
        )
    except IntegrityError:
        logger.info("duplicate message %s lost insert race", event.provider_message_id)
        return

    conversation.last_message_at = timestamp
    conversation.save(update_fields=["last_message_at"])


def _apply_status_event(event: InboundEvent) -> None:
    """A delivery receipt for a message we sent: move its status forward, and
    record what the platform says it charged."""
    if event.status is None:
        raise ValueError("status event without a status")

    message = Message.objects.filter(
        provider_message_id=event.provider_message_id
    ).first()
    if message is None:
        message = _adopt_unconfirmed_send(event)
    if message is None:
        # Receipts can outrun the send's DB commit, or reference messages
        # sent outside this app. Nothing to update either way.
        logger.info("status for unknown message %s", event.provider_message_id)
        return

    changed = []

    # Billing first, and deliberately outside the forward-only rule below.
    # Meta puts its pricing object on the ``sent`` receipt and on one of
    # delivered/read, and those can arrive in any order; a late ``sent``
    # still carries a verdict worth recording even though it cannot move the
    # status forward.
    if event.pricing:
        changed += _apply_pricing(message, event.pricing)

    # Forward-only: providers deliver receipts out of order (and retry old
    # ones), and "read" must never regress to "delivered".
    if status_rank(event.status.value) > status_rank(message.status):
        message.status = event.status.value
        changed.append("status")

    if changed:
        message.save(update_fields=changed)


#: How far back a receipt for an id no row carries may look for the send that
#: lost it. The first receipt ("sent") follows a send within seconds; the
#: window covers a slow webhook and stays short enough that a message sent to
#: the same customer from outside the CRM cannot be pinned on an old row.
UNCONFIRMED_SEND_WINDOW = timedelta(minutes=10)


def _adopt_unconfirmed_send(event: InboundEvent) -> Message | None:
    """Match a receipt whose id no row carries to the send that lost it.

    A send can reach WhatsApp and still leave the CRM without the message id:
    the provider timed out reading the answer (:class:`SendUnconfirmed`), or
    the write after the provider call failed. The row then sits on the clock
    while the customer reads the message, and every receipt for it is dropped
    as unknown. This is the repair: the newest id-less *queued* outbound row
    to the receipt's recipient, created within :data:`UNCONFIRMED_SEND_WINDOW`
    of the receipt, takes the id. Only ``queued`` rows qualify -- a ``failed``
    one is a send the platform refused outright, and a receipt for the same
    customer minutes later belongs to some other message.

    Returns the adopted row (status ``queued``, so the caller's forward-only
    rule then applies the receipt's verdict on top), or ``None`` when nothing
    qualifies: no recipient on the receipt, no such contact, no recent orphan.
    """
    if not event.to_number:
        return None
    contact = _find_contact(event.to_number)
    if contact is None:
        return None

    received_at = event.timestamp or timezone.now()
    orphan = (
        Message.objects.filter(
            conversation__contact=contact,
            direction=Message.OUTBOUND,
            provider_message_id__isnull=True,
            status=MessageStatus.QUEUED.value,
            timestamp__gte=received_at - UNCONFIRMED_SEND_WINDOW,
        )
        .order_by("-timestamp", "-pk")
        .first()
    )
    if orphan is None:
        return None

    orphan.provider_message_id = event.provider_message_id
    try:
        # A savepoint: the caller runs each event inside atomic(), and an
        # IntegrityError would otherwise poison that transaction.
        with transaction.atomic():
            orphan.save(update_fields=["provider_message_id"])
    except IntegrityError:
        # Two receipts for the same id, in concurrent webhooks, both picked
        # this row; the other one won. Read back what it wrote.
        return Message.objects.filter(provider_message_id=event.provider_message_id).first()

    logger.warning(
        "receipt %s adopted by message %s: its send was never confirmed",
        event.provider_message_id, orphan.pk,
    )
    return orphan


def _apply_pricing(message: Message, pricing_info: dict) -> list[str]:
    """Record the platform's billing verdict on ``message`` and correct the
    amount the CRM estimated. Returns the fields it changed.

    The estimate frozen at send time is the best the CRM can do beforehand;
    this is what actually happened, and it can differ three ways:

    * **Free after all.** ``type`` is ``free_customer_service`` or
      ``free_entry_point`` (or ``billable`` is false) -- the send rode an
      open window the CRM did not know about, most often the 72-hour free
      entry point that follows a Click-to-WhatsApp ad. The amount drops to
      zero.
    * **A different category.** Meta re-categorises templates on its own
      (a utility template judged to be marketing, say) and bills at *its*
      category. The amount is recomputed from that, at the rate card in
      force when the message was sent -- not today's card, so a message
      priced under an old card stays priced under it.
    * **Nothing to change.** The common case: Meta confirms the category and
      that it was billable, and the amount already says so.

    Never touches a failed message: nothing is charged for a send that did
    not arrive, and ``send_template`` has already zeroed it.
    """
    fields = []

    def record(name, value):
        if getattr(message, name) != value:
            setattr(message, name, value)
            fields.append(name)

    record("meta_pricing_type", pricing_info.get("type") or "")
    record("meta_pricing_category", pricing_info.get("category") or "")
    record("meta_pricing_model", pricing_info.get("model") or "")
    billable = pricing_info.get("billable")
    record("meta_billable", billable if isinstance(billable, bool) else None)

    # Only messages the CRM actually billed are worth reconciling: an
    # inbound row, or a free-form reply, carries NULL and stays NULL.
    if message.billed_amount is None or message.status == MessageStatus.FAILED.value:
        return fields

    pricing_type = (pricing_info.get("type") or "").strip()
    # ``type`` is absent from some payloads, so fall back to ``billable``;
    # with neither, assume Meta charged (the estimate stands).
    if pricing_type:
        is_free = pricing_type != "regular"
    elif isinstance(billable, bool):
        is_free = not billable
    else:
        is_free = False

    if is_free:
        if message.billed_amount != Decimal("0"):
            message.billed_amount = Decimal("0")
            fields.append("billed_amount")
        return fields

    # Billable, at Meta's category. Re-price only when that differs from
    # what the CRM assumed, and only for a category the rate card knows --
    # marketing_lite and referral_conversion are billed by mechanisms this
    # CRM does not implement, so their amounts are left as they are.
    category = (pricing_info.get("category") or "").strip()
    if not category or category == message.billed_category:
        return fields
    if category not in pricing.CATEGORIES:
        logger.info(
            "meta billed message %s as %r, which this CRM cannot price",
            message.provider_message_id,
            category,
        )
        return fields

    market = pricing.market_for(message.conversation.contact)
    # timezone.localdate() of the send, so an old message keeps the card it
    # was actually billed under.
    amount = pricing.rate_for(market, category, timezone.localdate(message.timestamp))
    if amount != message.billed_amount:
        logger.info(
            "meta re-categorised message %s from %s to %s: %s -> %s",
            message.provider_message_id,
            message.billed_category or "(none)",
            category,
            message.billed_amount,
            amount,
        )
        message.billed_amount = amount
        fields.append("billed_amount")
    if message.billed_category != category:
        message.billed_category = category
        fields.append("billed_category")
    return fields

def canonical_phone(phone: str) -> str:
    """A phone number in the one shape this app stores: ``+`` then digits.

    WhatsApp reports a ``wa_id`` with no ``+`` (``573001112233``) and people
    type numbers with spaces and dashes, so the same customer reaches the
    database under several spellings. Everything downstream compares phones
    as plain strings -- ``_upsert_contact``'s lookup, the wa.me link, the
    country flag -- so one customer with two spellings is two contacts, two
    conversation threads and a send to a number the provider rejects.

    Returns ``""`` for input with no digits at all, which callers treat as
    "nothing to match on".
    """
    digits = "".join(character for character in phone if character.isdigit())
    return f"+{digits}" if digits else ""


def _upsert_contact(phone: str, contact_name: str, channel: str) -> Client:
    """Find the Client for a phone number, creating one on first contact.

    ``phone`` is whichever side of the event is the customer -- ``from_number``
    for an inbound message, ``to_number`` for one of ours sent outside the
    Inbox (see :func:`_apply_outbound_event`).

    The lookup accepts the number as given *and* as stored: rows also arrive
    from outside this app (see the README's external writer contract), and a
    writer that stored the raw ``wa_id`` would otherwise get a duplicate
    contact every time the app itself saw the same customer. New contacts are
    always written canonically, so the ambiguity does not spread.
    """
    contact = _find_contact(phone)
    if contact is not None:
        return contact

    canonical = canonical_phone(phone)
    stored = canonical or phone
    name = contact_name.strip() or stored
    return Client.objects.create(
        first_name=name[:80],
        phone=stored,
        channel=_client_channel(channel),
        # +57 numbers get the Colombian flag in the CRM table; other prefixes
        # are left blank rather than guessed.
        country="CO" if stored.startswith("+57") else "",
    )


def _find_contact(phone: str) -> Client | None:
    """The Client stored under ``phone`` in any of its spellings, or None.

    Accepts the number as given *and* as canonical, with and without the
    ``+``: rows also arrive from outside this app (the README's external
    writer contract), and one that stored the raw ``wa_id`` must still be
    found. Ordered so an exact hit wins when a database somehow holds both.
    """
    canonical = canonical_phone(phone)
    candidates = [value for value in (phone, canonical, canonical.lstrip("+")) if value]
    return next(
        (
            found
            for value in candidates
            for found in Client.objects.filter(phone=value)[:1]
        ),
        None,
    )


def _client_channel(conversation_channel: str) -> str:
    """Map a conversation channel key onto Client's coarser channel choices
    (the CRM table doesn't distinguish DM surfaces from the brand)."""
    if conversation_channel.startswith("tiktok"):
        return "tiktok"
    if conversation_channel == "instagram-dm":
        return "instagram"
    return conversation_channel


def _get_or_create_open_conversation(contact: Client, channel: str) -> Conversation:
    """The active thread for this contact+channel, or a fresh one.

    Only open/pending threads are reused. Resolved threads stay closed
    history -- a new inbound after resolution starts a new conversation row,
    so per-thread metrics survive the customer coming back.
    """
    conversation = (
        Conversation.objects.filter(contact=contact, channel=channel)
        .exclude(status=Conversation.RESOLVED)
        .order_by("-last_message_at")
        .first()
    )
    if conversation is not None:
        return conversation
    return Conversation.objects.create(contact=contact, channel=channel)


# --- Tags -------------------------------------------------------------------
#
# Every surface that touches tags -- the row picker, the chat header, bulk
# actions, the Etiquetas admin -- goes through these functions, so rules like
# "archived tags leave history alone" live in exactly one place.


class TagNameTaken(Exception):
    """A tag with this name (case-insensitively) already exists."""


def _validate_tag_name(name: str, exclude_pk=None) -> str:
    name = name.strip()
    if not name:
        raise ValueError("El nombre de la etiqueta no puede estar vacío.")
    clash = Tag.objects.filter(name__iexact=name)
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        raise TagNameTaken(f"Ya existe una etiqueta llamada «{name}».")
    return name


def _validate_tag_color(color: str) -> str:
    valid = {key for key, _ in Tag.COLOR_CHOICES}
    if color not in valid:
        raise ValueError(f"Color desconocido: {color!r}")
    return color


def next_tag_color() -> str:
    """A palette token for a tag created without picking one (the picker's
    inline «Crear ...» path). Cycling by tag count spreads new tags across
    the palette instead of piling them onto one default; the color stays
    editable in the Etiquetas page."""
    palette = [key for key, _ in Tag.COLOR_CHOICES]
    return palette[Tag.objects.count() % len(palette)]


def create_tag(name: str, color: str | None = None, user=None) -> Tag:
    """Create a tag, enforcing the case-insensitive unique name.

    Raises :class:`TagNameTaken` on a duplicate and ``ValueError`` on an
    empty name or a color outside the preset palette. The DB constraint
    backs the name check up against races.
    """
    name = _validate_tag_name(name)
    color = _validate_tag_color(color) if color else next_tag_color()
    try:
        return Tag.objects.create(
            name=name,
            color=color,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
    except IntegrityError as exc:
        raise TagNameTaken(f"Ya existe una etiqueta llamada «{name}».") from exc


def update_tag(tag: Tag, name: str, color: str) -> Tag:
    """Rename/recolor a tag -- every pill referencing it updates at once,
    which is the point of tagging by FK instead of by string."""
    tag.name = _validate_tag_name(name, exclude_pk=tag.pk)
    tag.color = _validate_tag_color(color)
    tag.save(update_fields=["name", "color"])
    return tag


def set_tag_archived(tag: Tag, archived: bool) -> Tag:
    """Archive (or restore) a tag.

    Archiving is the only "delete": the tag vanishes from pickers and
    filters but every ConversationTag row survives, so tagged history stays
    exactly as it was. Never hard-delete a tag that is in use.
    """
    tag.is_archived = archived
    tag.save(update_fields=["is_archived"])
    return tag


def apply_tag(conversations, tag: Tag, user=None) -> int:
    """Apply ``tag`` to each conversation; returns how many actually gained it.

    Accepts any iterable/queryset of Conversations -- one row, or hundreds
    from a bulk action. Idempotent per conversation (already-tagged rows are
    skipped), and archived tags are refused: they exist only as history.
    """
    if tag.is_archived:
        raise ValueError("No se puede aplicar una etiqueta archivada.")

    tagged_by = user if getattr(user, "is_authenticated", False) else None
    applied = 0
    for conversation in conversations:
        _, created = ConversationTag.objects.get_or_create(
            conversation=conversation,
            tag=tag,
            defaults={"tagged_by": tagged_by},
        )
        applied += created
    return applied


def remove_tag(conversations, tag: Tag) -> int:
    """Take ``tag`` off each conversation; returns how many rows changed.

    Works on archived tags too -- an archived tag can still be *removed*
    from a chat (that's editing that chat), it just can't be newly applied.
    """
    deleted, _ = ConversationTag.objects.filter(
        conversation__in=conversations, tag=tag
    ).delete()
    return deleted


# --- Fake-provider pump -----------------------------------------------------


def pump_provider_events() -> None:
    """Let a pull-based provider (the fake one) deliver its pending events.

    Real providers push webhooks; the fake provider has no server to push
    from, so the Inbox poll endpoints call this instead. Events flow through
    :func:`process_inbound_events` exactly like a webhook's would. A no-op
    for providers without ``pending_status_events``.
    """
    provider = get_provider()
    pending = getattr(provider, "pending_status_events", None)
    if pending is None:
        return
    events = pending()
    if events:
        process_inbound_events(events)
