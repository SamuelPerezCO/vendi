"""Domain models for the CRM."""

from django.conf import settings
from django.db import models


class Client(models.Model):
    """A person in the CRM's client list.

    Fields map directly onto the Clientes table columns: name, phone (with the
    country driving a flag), mail and canal. WhatsApp availability is derived
    from ``channel`` rather than stored separately, so the two can't disagree.
    """

    # Channel keys deliberately match core.inbox.CANALES so a client's channel
    # and an inbox conversation filter refer to the same thing.
    CHANNEL_CHOICES = [
        ("whatsapp", "WhatsApp"),
        ("messenger", "Messenger"),
        ("instagram", "Instagram"),
        ("facebook", "Facebook"),
        ("tiktok", "TikTok"),
    ]

    first_name = models.CharField("nombres", max_length=80)
    last_name = models.CharField("apellidos", max_length=80, blank=True)

    phone = models.CharField("teléfono", max_length=20, help_text="E.164, e.g. +573167687288")
    country = models.CharField(
        "país", max_length=2, blank=True, help_text="ISO 3166-1 alpha-2, drives the flag"
    )

    email = models.EmailField("mail", blank=True)
    channel = models.CharField("canal", max_length=20, choices=CHANNEL_CHOICES, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    #: When the automatic welcome was sent to this person, or NULL if never.
    #: A timestamp rather than a boolean so it is also an audit trail, and the
    #: column is what makes the welcome fire exactly once: the send is guarded
    #: by an UPDATE ... WHERE welcomed_at IS NULL, so a webhook retry or two
    #: messages arriving together cannot produce two greetings.
    welcomed_at = models.DateTimeField("bienvenida enviada", null=True, blank=True)

    class Meta:
        verbose_name = "cliente"
        verbose_name_plural = "clientes"
        ordering = ["first_name", "last_name"]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def initials(self) -> str:
        """Up to two letters for the Inbox's avatar circles."""
        letters = f"{self.first_name[:1]}{self.last_name[:1]}".upper()
        return letters or "?"

    @property
    def icon_template(self) -> str:
        """Brand mark for this client's channel, or ``""`` when there is none
        to draw.

        Empty rather than a guess: the Clientes table used to build the
        template path out of the column itself, so a channel this app does
        not know -- and rows do arrive from outside it, see the README's
        external writer contract -- raised TemplateDoesNotExist and took the
        whole CRM section down. The label still renders; only the icon is
        dropped.
        """
        if self.channel not in dict(self.CHANNEL_CHOICES):
            return ""
        return f"icons/brands/{self.channel}.svg"

    @property
    def flag(self) -> str:
        """The country as a flag emoji, built from regional indicator symbols.

        Avoids shipping flag images; returns "" for a missing or malformed code
        so the template can just print it.
        """
        code = (self.country or "").upper()
        if len(code) != 2 or not code.isalpha():
            return ""
        return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in code)

    # No whatsapp_url / has_whatsapp here. They built a wa.me link for an
    # "Iniciar conversación" that opened WhatsApp on the operator's own
    # device. This business has no handset -- its number lives on the Meta
    # API and every message leaves through this app -- so that link was a
    # dead end, and the properties behind it are gone rather than left
    # lying around to be wired up again. Writing to someone starts at the
    # Inbox's Nuevo chat modal (?nuevo=<client id>), which sends an approved
    # plantilla through the provider.


class Product(models.Model):
    """A product in the Mi comercio catalogue.

    Fields map directly onto the Productos table columns. Categoría and Marca
    are plain text for now -- they become foreign keys once the Categorías and
    Marcas pages define real models. "Sincronizado con" (the sales channels a
    product is synced to) deliberately has *no* field yet: it will be an M2M to
    a SalesChannel model once channel integrations exist, and the table renders
    an empty cell until then.
    """

    # The Productos tabs filter by these values; the tab-slug -> status
    # mapping lives in core.comercio._TAB_STATUS, next to the tab list itself
    # (the slugs are plural -- "activos" -- while these keys are singular).
    STATUS_CHOICES = [
        ("activo", "Activo"),
        ("inactivo", "Inactivo"),
    ]

    name = models.CharField("nombre", max_length=120)
    stock = models.PositiveIntegerField("stock", default=0)
    price = models.DecimalField("precio", max_digits=10, decimal_places=2)

    category = models.CharField("categoría", max_length=80, blank=True)
    brand = models.CharField("marca", max_length=80, blank=True)

    status = models.CharField(
        "estado", max_length=10, choices=STATUS_CHOICES, default="activo"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "producto"
        verbose_name_plural = "productos"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ClientList(models.Model):
    """A named group of clients -- one row in the "Lista de clientes" table.

    "Número de contactos" is derived from the ``clients`` M2M (annotated in
    the view) so the count can never disagree with the list's actual members.
    ``created_by`` is plain text until the app grows real users/auth, at which
    point it becomes a foreign key.
    """

    name = models.CharField("nombre del grupo", max_length=120)
    clients = models.ManyToManyField(
        Client, related_name="client_lists", blank=True, verbose_name="clientes"
    )

    created_by = models.CharField("creado por", max_length=80, blank=True)
    created_at = models.DateTimeField("fecha", auto_now_add=True)

    class Meta:
        verbose_name = "lista de clientes"
        verbose_name_plural = "listas de clientes"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class MessageTemplate(models.Model):
    """A WhatsApp message template -- one row in the Plantillas table and the
    product of the Crear plantilla editor.

    ``is_active`` (Activo) is the account's own on/off toggle; ``status``
    (Estado) is the WhatsApp approval verdict -- separate fields because they
    move independently: an approved template can be switched off. The
    Desactivadas tab filters on the toggle, the other tabs on the status.

    The choice lists here are flat unions so stored values always display;
    which sub-types each *category* actually offers, the language list and
    the editor's validation all live in core.plantillas. ``team`` and
    ``created_by`` stay plain text until the app grows real users/auth (same
    stance as ClientList.created_by).
    """

    CATEGORY_CHOICES = [
        ("marketing", "Marketing"),
        ("utility", "Utility"),
        ("authentication", "Autenticación"),
    ]

    SUB_TYPE_CHOICES = [
        ("custom", "Mensaje personalizado"),
        ("limited_time_offer", "Oferta de tiempo limitado"),
        ("carousel", "Carrusel"),
        ("auth_code", "Código de autenticación"),
    ]

    HEADER_CHOICES = [
        ("none", "Ninguno"),
        ("text", "Texto"),
        ("image", "Imagen"),
        ("video", "Video"),
        ("document", "Documento"),
    ]

    STATUS_CHOICES = [
        ("pendiente", "Pendiente"),
        ("aceptada", "Aceptada"),
        ("rechazada", "Rechazada"),
    ]

    # Meta constraint, not a style choice: lowercase, digits and _ only.
    # The regex itself lives in core.plantillas.NAME_RE (single source).
    name = models.CharField("nombre", max_length=120)
    category = models.CharField(
        "categoría", max_length=20, choices=CATEGORY_CHOICES, default="marketing"
    )
    sub_type = models.CharField(
        "tipo", max_length=30, choices=SUB_TYPE_CHOICES, default="custom"
    )
    language = models.CharField("idioma", max_length=10, default="es")
    team = models.CharField("equipo", max_length=80, blank=True)

    header_type = models.CharField(
        "cabecera", max_length=10, choices=HEADER_CHOICES, default="none"
    )
    header_text = models.CharField("texto de cabecera", max_length=60, blank=True)
    header_media = models.FileField(
        "archivo de cabecera", upload_to="plantillas/", blank=True
    )

    body = models.TextField("cuerpo", blank=True)
    #: One sample string per {{n}} variable, element i pairing with {{i+1}} --
    #: Meta requires example values at submission time and the preview
    #: substitutes them live.
    body_sample_values = models.JSONField(
        "valores de ejemplo", default=list, blank=True
    )
    footer = models.CharField("pie de página", max_length=60, blank=True)
    #: List of {"type": "quick_reply"|"url"|"phone", "text": ..., ...} dicts.
    buttons = models.JSONField("botones", default=list, blank=True)

    # --- Autenticación only -------------------------------------------------
    #
    # An authentication plantilla stores none of the copy above, because
    # WhatsApp does not accept any: it writes and localizes the wording of an
    # OTP message itself and takes only these three settings. ``body`` still
    # holds WhatsApp's own sentence for this language (see
    # core.plantillas.AUTH_BODIES) so the Inbox's send dialog and the
    # conversation thread have something true to show -- it is never
    # submitted for approval.

    auth_security_recommendation = models.BooleanField(
        "añadir recomendación de seguridad", default=True
    )
    auth_code_expiration_minutes = models.PositiveSmallIntegerField(
        "el código vence en (minutos)", null=True, blank=True
    )
    auth_button_text = models.CharField(
        "texto del botón de copiado", max_length=25, blank=True
    )

    is_active = models.BooleanField("activo", default=True)
    status = models.CharField(
        "estado", max_length=10, choices=STATUS_CHOICES, default="pendiente"
    )
    rejection_reason = models.TextField("motivo de rechazo", blank=True)

    #: The id Meta assigned when the plantilla was submitted for approval.
    #: Blank means it was never submitted (no META_WABA_ID at save time, or
    #: the submission failed) -- the status sync matches by (name, language)
    #: anyway, so a template created in Meta's own console still reconciles.
    provider_template_id = models.CharField(
        "id en el proveedor", max_length=64, blank=True
    )
    #: When the approval state was last read back from the provider. Null
    #: until the first sync; the Plantillas page shows it so "Pendiente"
    #: reads as "pending as of <when>", not as a guess.
    status_synced_at = models.DateTimeField(
        "estado sincronizado", null=True, blank=True
    )

    created_by = models.CharField("creado por", max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "plantilla de WhatsApp"
        verbose_name_plural = "plantillas de WhatsApp"
        ordering = ["name"]
        constraints = [
            # Meta scopes template names per language, so the pair is the key.
            models.UniqueConstraint(
                fields=["name", "language"], name="unique_template_name_per_language"
            ),
        ]

    def __str__(self) -> str:
        return self.name

class QuickReply(models.Model):
    """A canned answer the composer's Respuestas rápidas picker sends in one
    click -- the account's own, kept apart from WhatsApp plantillas.

    A plantilla (:class:`MessageTemplate`) is Meta's concept: approved text
    with numbered variables, the only thing allowed outside the 24h window.
    A quick reply is the team's: free text written once ("Nuestro horario es
    de 9 a 6"), optionally with an image (a price list, the store front),
    sent inside the window like any typed message. The picker used to list
    plantillas because nothing else existed; now it lists these, and
    plantillas keep their real job in the "Enviar plantilla" flow.

    ``image`` goes through default storage (Vercel Blob in production), so
    the URL the provider is handed is public -- Meta fetches it by link.
    """

    title = models.CharField("título", max_length=80)
    body = models.TextField("texto", blank=True)
    # FileField, not ImageField: the latter needs Pillow, which this project
    # doesn't ship (MessageTemplate.header_media makes the same call). The
    # form restricts uploads to image types instead.
    image = models.FileField("imagen", upload_to="respuestas/", blank=True)

    #: Off means hidden from the picker without deleting the text.
    is_active = models.BooleanField("activa", default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quick_replies_created",
        verbose_name="creada por",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "respuesta rápida"
        verbose_name_plural = "respuestas rápidas"
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title

    @property
    def has_image(self) -> bool:
        return bool(self.image)


class MessagingSettings(models.Model):
    """The account's messaging automations, as one row.

    A singleton (:meth:`load` keeps it that way) rather than three models,
    because these are three switches on one thing -- what the CRM does on its
    own when a customer writes -- and a screen that reads them all wants one
    query, not three. The alternative, a key/value settings table, buys
    flexibility this app has no use for and costs every reader a lookup by
    string.

    Nothing here changes how a message is *stored*; each field only decides
    whether the app acts by itself, and each defaults to "no". An account
    that never opens these screens behaves exactly as before.
    """

    #: Round-robin position for auto-assignment: the index into
    #: core.agents.agent_users() the next conversation starts looking from.
    #: Stored rather than derived so the rotation survives a restart, and so
    #: two conversations arriving together cannot both pick the same agent
    #: (the read and the bump happen in one transaction).
    ASSIGN_ROUND_ROBIN = "round_robin"
    ASSIGN_CHOICES = [(ASSIGN_ROUND_ROBIN, "Por turnos entre los agentes activos")]

    WIDGET_POSITIONS = [
        ("right", "Abajo a la derecha"),
        ("left", "Abajo a la izquierda"),
    ]

    # --- Mensajes de bienvenida ---------------------------------------------
    welcome_enabled = models.BooleanField("bienvenida activa", default=False)
    welcome_body = models.TextField("mensaje de bienvenida", blank=True)

    # --- Asignación automática ----------------------------------------------
    assign_enabled = models.BooleanField("asignación automática activa", default=False)
    assign_strategy = models.CharField(
        "estrategia", max_length=20, choices=ASSIGN_CHOICES, default=ASSIGN_ROUND_ROBIN
    )
    assign_cursor = models.PositiveIntegerField("turno actual", default=0)

    # --- Widget de WhatsApp --------------------------------------------------
    widget_phone = models.CharField("teléfono del widget", max_length=20, blank=True)
    widget_greeting = models.CharField(
        "saludo del widget", max_length=140, blank=True,
        default="Hola, quiero más información.",
    )
    widget_label = models.CharField(
        "texto del botón", max_length=40, blank=True, default="Escríbenos",
    )
    widget_position = models.CharField(
        "posición", max_length=10, choices=WIDGET_POSITIONS, default="right"
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "configuración de mensajería"
        verbose_name_plural = "configuración de mensajería"

    def __str__(self) -> str:
        return "Configuración de mensajería"

    @classmethod
    def load(cls) -> "MessagingSettings":
        """The one row, created on first use.

        ``pk=1`` is pinned deliberately: get_or_create on an unconstrained
        table would race two requests into two rows, and every reader after
        that would see whichever one it happened to fetch.
        """
        row, _ = cls.objects.get_or_create(pk=1)
        return row

    @property
    def widget_url(self) -> str:
        """The wa.me link the widget button opens, greeting pre-filled."""
        from urllib.parse import quote

        digits = "".join(c for c in self.widget_phone if c.isdigit())
        if not digits:
            return ""
        base = f"https://wa.me/{digits}"
        return f"{base}?text={quote(self.widget_greeting)}" if self.widget_greeting else base


class CalendarEvent(models.Model):
    """One entry in the CRM's Mi calendario.

    ``contact`` is the reason a calendar lives inside a CRM: an event links
    to a client ("llamada con Camila") so the record is one click away. The
    user FKs are nullable following the Conversation precedent -- the app
    has no real login yet, so events created from the UI carry no user.

    Times are stored in UTC (USE_TZ); entry and display happen in
    core.calendario.CALENDAR_TZ. ``event_type`` picks the color, reusing a
    tag palette pair -- see core.calendario.EVENT_TYPES.
    """

    TYPE_CHOICES = [
        ("llamada", "Llamada"),
        ("reunion", "Reunión"),
        ("seguimiento", "Seguimiento"),
        ("otro", "Otro"),
    ]

    title = models.CharField("título", max_length=120)
    description = models.TextField("descripción", blank=True)

    start = models.DateTimeField("inicio")
    end = models.DateTimeField("fin")
    #: All-day events span whole days: start at midnight, end exclusive.
    all_day = models.BooleanField("todo el día", default=False)

    contact = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calendar_events",
        verbose_name="cliente",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calendar_events",
        verbose_name="asignado a",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_calendar_events",
        verbose_name="creado por",
    )

    event_type = models.CharField(
        "tipo", max_length=20, choices=TYPE_CHOICES, default="reunion"
    )
    reminder_minutes_before = models.PositiveIntegerField(
        "recordatorio (minutos antes)", null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "evento de calendario"
        verbose_name_plural = "eventos de calendario"
        ordering = ["start"]
        indexes = [
            # The grid's query: events in a window, per advisor.
            models.Index(fields=["start", "assigned_to"], name="calendar_start_advisor_idx"),
        ]

    def __str__(self) -> str:
        return self.title


class StoredFile(models.Model):
    """One uploaded file, kept in the database instead of on disk.

    Vercel's functions have no writable filesystem, so ``MEDIA_ROOT`` is not
    an option in production. The project's first answer to that is Vercel
    Blob (``core.storage.VercelBlobStorage``); this model backs the fallback
    for a deployment that has no Blob store connected, where the database is
    the only writable thing the app has. See ``core.storage.DatabaseStorage``.

    ``name`` is the storage key ("respuestas/foto.png") and stays exactly what
    the caller asked for, so ``exists()`` can answer about a deterministic
    name -- the Meta webhook depends on that to stay idempotent across
    retries. ``token`` is what appears in the URL instead: the keys are
    guessable ("plantillas/logo.png"), and these files are served without
    authentication because WhatsApp itself has to fetch them, so the public
    handle is random rather than the name.
    """

    name = models.CharField("nombre", max_length=255, unique=True)
    #: Random public handle; see the class docstring for why it is not `name`.
    token = models.CharField("token", max_length=32, unique=True)
    content = models.BinaryField("contenido")
    content_type = models.CharField("tipo", max_length=100, blank=True)
    size = models.PositiveIntegerField("tamaño", default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "archivo almacenado"
        verbose_name_plural = "archivos almacenados"
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return self.name
