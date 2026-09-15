"""Conversations and messages -- the data behind the Inbox screen.

The contact side reuses :class:`core.models.Client`: the person you chat with
in the Inbox *is* the person in the CRM's Clientes table. Webhook processing
upserts Clients by phone number (see ``services.process_inbound_events``).
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from .providers.types import MEDIA_PLACEHOLDERS, MessageStatus

#: How long after a customer's last message WhatsApp allows free-form replies.
#: Outside it only pre-approved templates may be sent -- a platform rule, so
#: it is enforced in ``services.send_message``, not left to the UI.
SERVICE_WINDOW = timedelta(hours=24)


class Tag(models.Model):
    """A user-created label for conversations ("CLIENTE NUEVO", "VENTA
    EFECTIVA"...).

    Tags are runtime data, not code: users invent them, name them and pick a
    color in the Etiquetas page -- no choices list to edit, no migration to
    run. Colors are palette *tokens* rather than raw hex: each token maps to
    a --tag-<token>-bg/-fg CSS variable pair (static/css/tags.css), so every
    pill stays readable and a future dark theme restyles all tags at once.

    Deletion is archiving: an archived tag disappears from pickers but stays
    on every conversation it was ever applied to. Removing a tag from 500
    chats' history because someone tidied the picker would silently rewrite
    the past.
    """

    # The full preset palette. Users pick from these swatches only --
    # arbitrary hex is how unreadable pills happen.
    COLOR_CHOICES = [
        ("green", "Verde"),
        ("yellow", "Amarillo"),
        ("purple", "Morado"),
        ("blue", "Azul"),
        ("red", "Rojo"),
        ("orange", "Naranja"),
        ("teal", "Turquesa"),
        ("pink", "Rosado"),
        ("indigo", "Índigo"),
        ("gray", "Gris"),
    ]

    name = models.CharField("nombre", max_length=40)
    color = models.CharField(
        "color", max_length=10, choices=COLOR_CHOICES, default="gray"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tags_created",
        verbose_name="creada por",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    is_archived = models.BooleanField("archivada", default=False)

    class Meta:
        verbose_name = "etiqueta"
        verbose_name_plural = "etiquetas"
        ordering = ["name"]
        constraints = [
            # Case-insensitive: "ventas" and "VENTAS" are the same label.
            # TODO(tenancy): there is no organization/account model yet, so
            # names are globally unique. When tenancy lands, add the org FK
            # here and scope this constraint to (org, Lower(name)).
            models.UniqueConstraint(Lower("name"), name="tag_name_ci_unique"),
        ]

    def __str__(self) -> str:
        return self.name


class Conversation(models.Model):
    """One thread with one client on one channel.

    A client can have several rows over time (a resolved WhatsApp thread and a
    fresh one; a WhatsApp and an Instagram thread in parallel) but only one
    *active* thread per channel -- ``process_inbound_events`` appends to an
    open/pending conversation before it ever creates a new one.
    """

    # Keys deliberately match core.inbox.CANALES filter keys, so an inbox
    # channel filter is a straight ``channel=key`` lookup. (core.models.Client
    # keeps its own coarser channel field for the CRM table; this one says
    # where *this thread* lives.)
    CHANNEL_CHOICES = [
        ("whatsapp", "WhatsApp"),
        ("messenger", "Messenger"),
        ("instagram-dm", "Instagram DM"),
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("tiktok-dm", "Tiktok DM"),
        ("tiktok-coment", "Tiktok comentarios"),
    ]

    OPEN, PENDING, RESOLVED = "open", "pending", "resolved"
    STATUS_CHOICES = [
        (OPEN, "Abierta"),
        (PENDING, "Por resolver"),
        (RESOLVED, "Resuelta"),
    ]

    contact = models.ForeignKey(
        "core.Client",
        on_delete=models.CASCADE,
        related_name="conversations",
        verbose_name="cliente",
    )
    channel = models.CharField(
        "canal", max_length=20, choices=CHANNEL_CHOICES, default="whatsapp"
    )
    status = models.CharField(
        "estado", max_length=10, choices=STATUS_CHOICES, default=OPEN
    )

    # NULL = "Sin asignar" in the inbox nav; set = it shows in that user's
    # "Tu inbox".
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
        verbose_name="asignada a",
    )

    #: Timestamp of the newest message either direction -- the list sort key.
    last_message_at = models.DateTimeField(null=True, blank=True)

    #: Timestamp of the newest *inbound* message. Denormalized (maintained by
    #: ``services``) so the 24h-window check and the conversation list don't
    #: re-query the messages table per row.
    last_inbound_at = models.DateTimeField(null=True, blank=True)

    unread_count = models.PositiveIntegerField(default=0)

    # Through-model rather than a bare M2M so every application of a tag
    # records who and when (see ConversationTag) -- auditable tagging.
    tags = models.ManyToManyField(
        Tag,
        through="ConversationTag",
        related_name="conversations",
        blank=True,
        verbose_name="etiquetas",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "conversación"
        verbose_name_plural = "conversaciones"
        # Newest activity first -- the conversation list's natural order.
        ordering = ["-last_message_at"]
        indexes = [
            # The list query: filter by status, order by recency.
            models.Index(fields=["status", "last_message_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.contact} · {self.get_channel_display()}"

    @property
    def is_within_24h_window(self) -> bool:
        """Whether WhatsApp's customer-service window is currently open.

        The window opens (and re-opens) with every *inbound* message. A
        conversation the customer has never written to -- or not in 24h --
        gets ``False``: only a template send may (re)start it.

        ``last_inbound_at`` is denormalized bookkeeping this app maintains in
        ``services``, but rows also arrive from outside it (see the external
        writer contract in the README). A writer that inserts the message and
        forgets the UPDATE would otherwise leave the composer disabled
        forever, with nothing in the UI able to reopen it -- so a NULL falls
        back to asking the messages table. That costs one query, and only for
        the conversation actually being opened: the column still answers on
        its own whenever it is set, which is what keeps the list query flat.
        """
        last_inbound = self.last_inbound_at
        if last_inbound is None and self.pk is not None:
            last_inbound = (
                self.messages.filter(direction=Message.INBOUND)
                .order_by("-timestamp")
                .values_list("timestamp", flat=True)
                .first()
            )
        if last_inbound is None:
            return False
        return timezone.now() - last_inbound < SERVICE_WINDOW

    #: Shown when ``channel`` holds something this app has no brand mark for.
    UNKNOWN_CHANNEL_ICON = "icons/message-circle.svg"

    @property
    def icon_template(self) -> str:
        """Brand-mark template for this channel, for the conversation list.

        The two TikTok surfaces share one brand mark; everything else has a
        file named after its key (same files core.inbox.CANALES points at).

        An unrecognised value gets the generic mark instead of a path built
        from it. This used to interpolate the column straight into a template
        name, so a single row written with a channel like 'wa' or 'WhatsApp'
        raised TemplateDoesNotExist and took down the whole Inbox -- the list,
        the open thread and the poll -- for everyone, with no way to fix it
        short of editing the database.
        """
        if self.channel not in dict(self.CHANNEL_CHOICES):
            return self.UNKNOWN_CHANNEL_ICON
        name = "tiktok" if self.channel.startswith("tiktok") else self.channel
        return f"icons/brands/{name}.svg"


class ConversationTag(models.Model):
    """One tag on one conversation, with the audit trail of its application.

    This is the M2M through table -- explicit so "who tagged this chat, and
    when" is answerable, which a plain ManyToManyField throws away.
    """

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="conversation_tags"
    )
    tag = models.ForeignKey(
        Tag, on_delete=models.CASCADE, related_name="conversation_tags"
    )

    tagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tags_applied",
    )
    tagged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "etiqueta de conversación"
        verbose_name_plural = "etiquetas de conversación"
        constraints = [
            # The same tag on the same chat twice means nothing -- one row.
            models.UniqueConstraint(
                fields=["conversation", "tag"], name="conversationtag_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tag} → {self.conversation}"


class Message(models.Model):
    """One message inside a conversation, either direction."""

    INBOUND, OUTBOUND = "inbound", "outbound"
    DIRECTION_CHOICES = [(INBOUND, "Entrante"), (OUTBOUND, "Saliente")]

    STATUS_CHOICES = [(s.value, s.value) for s in MessageStatus]

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES)

    body = models.TextField(blank=True)
    media_url = models.URLField(blank=True)

    #: Kind of attachment behind ``media_url`` (``image``, ``video``,
    #: ``audio``, ``document``, ``sticker`` -- see ``InboundEvent.media_type``).
    #: Decides whether the thread renders the media inline or as a download
    #: link. Blank for text-only messages and for rows from before the field
    #: existed (those keep the generic link).
    media_type = models.CharField(max_length=16, blank=True)

    #: Delivery lifecycle; only meaningful outbound (inbound rows are stored
    #: as ``delivered`` -- they reached us, by definition).
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=MessageStatus.QUEUED.value
    )

    #: The provider's id -- unique so webhook retries can't insert the same
    #: message twice (the DB backs up the application-level check), and what
    #: status callbacks use to find their message. NULL for outbound rows
    #: whose send failed before the provider assigned an id.
    provider_message_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True
    )

    #: Provider-reported time when available, receipt/send time otherwise.
    timestamp = models.DateTimeField(default=timezone.now)

    #: Who wrote it, for outbound messages sent from the Inbox.
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_messages",
    )


    template = models.ForeignKey(
        "core.MessageTemplate",
        # SET_NULL: deleting the plantilla writes NULL into this column on
        # every message that used it; the message rows stay. ``null=True``
        # allows that NULL in the database, ``blank=True`` lets an admin form
        # leave the field empty.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        # The reverse side of the relation: ``plantilla.sent_messages.all()``
        # lists every message sent with that plantilla. The same name as
        # ``sent_by`` above is fine because each lives on a different model
        # (User.sent_messages vs MessageTemplate.sent_messages).
        related_name="sent_messages",
        # The label the admin shows for this field ("Plantilla"). A keyword
        # here because a ForeignKey's first positional argument is the target
        # model, unlike the plain fields below.
        verbose_name="plantilla",
    )

    # --- What this message cost -------------------------------------------
    #
    # Frozen at send time, never recomputed: WhatsApp's price list changes,
    # and a rate change must not rewrite what last month cost. NULL means
    # "not billed" -- every inbound message and every free-form reply inside
    # the 24h window. Zero means billed at nothing (a failed send, or a rule
    # that made it free), which is a different fact and stays distinguishable.

    # Who writes and reads these three columns: ``services.send_template``
    # fills them from the ``pricing.Quote`` it computed (category, amount,
    # currency) in the same ``Message.objects.create`` call that stores the
    # text, so a row never exists half-priced; if the provider then errors it
    # sets ``billed_amount`` to zero. ``pricing.spent_between`` sums
    # ``billed_amount`` for the monthly budget check, the admin lists and
    # filters on them, and the Inbox bubble (chat_messages.html) and the send
    # dialog's confirmation (send_sent.html) print amount + currency.

    #: The category the send was billed as, at send time
    #: (marketing/utility/authentication -- see messaging.pricing).
    # Deliberately no ``choices=`` (see ``billed_category_display`` below).
    # The first positional argument of a plain field is its verbose_name, the
    # label the admin shows. Empty string, not NULL, when the message was
    # never billed -- Django's convention for optional text columns.
    billed_category = models.CharField("categoría facturada", max_length=20, blank=True)
    #: Amount charged for this message, in ``billed_currency``. Six decimals
    #: because per-message rates are quoted in fractions of a cent.
    # A DecimalField comes back as ``decimal.Decimal`` in Python, never as a
    # float: 0.0125 has no exact binary representation, so float sums drift
    # by fractions of a cent and a comparison against the budget would be
    # off. ``max_digits=12, decimal_places=6`` allows up to 999999.999999.
    # This is the one nullable column of the three: ``null=True`` is what
    # makes the NULL ("not billed") vs zero ("billed at nothing") distinction
    # above possible, and what the partial index in Meta keys on.
    billed_amount = models.DecimalField(
        "importe", max_digits=12, decimal_places=6, null=True, blank=True
    )
    #: ISO 4217 code ("USD"), hence ``max_length=3``; comes from
    #: ``pricing.currency()`` (the MESSAGING_CURRENCY setting, "USD" by
    #: default). Stored per row so the amount stays readable even if the
    #: configured currency changes later. Empty when not billed.
    billed_currency = models.CharField("moneda", max_length=3, blank=True)
    #: True when this send was priced at the market's *Service* rate rather
    #: than its own category's -- a utility template delivered inside an open
    #: 24-hour window. Its own column rather than ``billed_category ==
    #: "service"`` because the delivery receipt rewrites that field (Meta
    #: reports such a send under its template category), and the monthly free
    #: service allowance is counted from these rows: a marker the
    #: reconciliation can flip would make the count drift.
    billed_as_service = models.BooleanField("cobrado como servicio", default=False)

    # --- What Meta says it cost ------------------------------------------
    #
    # The three columns above are the CRM's own estimate, frozen when the
    # message was created. These are the platform's verdict, filled in when
    # its delivery receipt arrives (``services._apply_status_event``), and
    # they are the reason the estimate can be corrected rather than trusted:
    # Meta charges on delivery, not on send, and at the category *it* has
    # assigned the plantilla -- it re-categorises templates on its own.
    #
    # Blank on every message until a provider reports billing, which today
    # means every provider but Meta, and every message sent before this
    # existed. Meta's own object carries no amount, only which rate bucket
    # applied, so the money stays the CRM's arithmetic over that bucket.

    #: ``regular`` (billable), ``free_customer_service`` or
    #: ``free_entry_point``. Meta's preferred billable test, now that
    #: ``billable`` is on its way out.
    meta_pricing_type = models.CharField(
        "tipo de precio (Meta)", max_length=32, blank=True
    )
    #: The rate Meta actually applied: marketing, utility, authentication,
    #: authentication-international, service, marketing_lite,
    #: referral_conversion. Stored verbatim, hyphen and all.
    meta_pricing_category = models.CharField(
        "categoría facturada (Meta)", max_length=32, blank=True
    )
    #: ``PMP`` (per-message, the default since 2025-07-01) or ``CBP``.
    meta_pricing_model = models.CharField(
        "modelo de precio (Meta)", max_length=8, blank=True
    )
    #: Meta's own billable flag. NULL until reported; being deprecated by
    #: Meta in favour of ``meta_pricing_type``, kept because it is still the
    #: most direct statement of "did this cost anything".
    meta_billable = models.BooleanField("facturable (Meta)", null=True, blank=True)

    class Meta:
        verbose_name = "mensaje"
        verbose_name_plural = "mensajes"
        # Chronological -- the order a chat thread renders in.
        ordering = ["timestamp", "id"]
        indexes = [
            # The thread query: one conversation's messages in order.
            models.Index(fields=["conversation", "timestamp"]),
            # The spend query (messaging.pricing.spent_between): billed rows
            # in a date range. Partial, because the billed ones are the small
            # minority of a busy inbox's messages -- the condition becomes a
            # WHERE clause on the index, so only billed rows are entered in
            # it. Django insists on an explicit name for a conditional index.
            # Created in the database by migration 0004_message_billing.
            models.Index(
                fields=["timestamp"],
                condition=models.Q(billed_amount__isnull=False),
                name="message_billed_timestamp_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.direction}] {self.body[:40]}"

    @property
    def cost_is_confirmed(self) -> bool:
        """Whether ``billed_amount`` reflects Meta's verdict or is still the
        CRM's pre-send estimate.

        False for every message until its delivery receipt carries a pricing
        object.
        """
        return bool(self.meta_pricing_type or self.meta_pricing_category)

    @property
    def billed_category_display(self) -> str:
        """The Spanish label for what this send was billed as.

        Not ``get_billed_category_display()``: the field carries no choices
        on purpose (it records whatever category the plantilla had at send
        time, and a category list added by Meta later must still store), so
        the labels are looked up from the plantilla model instead --
        imported inside the property to keep this module free of a top-level
        ``core.models`` import, the same stance the "app.Model" strings above
        take.

        Read by templates/partials/plantillas/send_sent.html as
        ``{{ sent_message.billed_category_display }}`` -- ``@property`` is
        what lets a template read it like a plain attribute, no call needed.
        """
        # The import runs when the property is used, not when this module
        # loads. Python caches imported modules, so after the first access it
        # costs a couple of dictionary lookups. This file otherwise refers to
        # core models only by "app.Model" strings (``template`` above,
        # ``Conversation.contact``), so core.models is never imported at the
        # top of it.
        from core.models import MessageTemplate

        # CATEGORY_CHOICES is a list of (stored value, label) pairs; dict()
        # turns it into {"marketing": "Marketing", ...}. ``.get`` with the raw
        # value as the default means an unknown or empty category comes back
        # as is instead of raising -- the row still renders.
        labels = dict(MessageTemplate.CATEGORY_CHOICES)
        return labels.get(self.billed_category, self.billed_category)

    @property
    def is_outbound(self) -> bool:
        return self.direction == self.OUTBOUND

    @property
    def is_inline_image(self) -> bool:
        """Whether the thread should show the media itself rather than a
        download link. Stickers are WebP -- browsers render them natively."""
        return bool(self.media_url) and self.media_type in ("image", "sticker")

    @property
    def display_body(self) -> str:
        """Body for the chat bubble. When the image itself renders inline,
        the "[imagen]"/"[sticker]" placeholder would just repeat it as text --
        real captions still show. The raw ``body`` keeps the placeholder so
        the conversation list preview reads as something."""
        if self.is_inline_image and self.body == MEDIA_PLACEHOLDERS.get(self.media_type):
            return ""
        return self.body

    @property
    def status_icon_template(self) -> str:
        """Tick/clock/alert icon for the outbound status indicator.

        Total on purpose. This was a bare dict subscript, so one row carrying
        a status from someone else's vocabulary ('accepted', 'error', '') --
        which is exactly what an external writer inserts (see the README's
        contract) -- raised KeyError and 500'd that entire conversation:
        the full page, the swap and the five-second poll alike.

        The fallback is the alert icon rather than a tick: an unrecognised
        state is not a delivery anyone confirmed, and drawing a tick would
        claim one.
        """
        return {
            MessageStatus.QUEUED.value: "icons/clock.svg",
            MessageStatus.SENT.value: "icons/check.svg",
            MessageStatus.DELIVERED.value: "icons/check-check.svg",
            MessageStatus.READ.value: "icons/check-check.svg",
            MessageStatus.FAILED.value: "icons/alert-circle.svg",
        }.get(self.status, "icons/alert-circle.svg")
