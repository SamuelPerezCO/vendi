"""Meta (WhatsApp Cloud API) provider -- the official WhatsApp integration.

Talks to the Graph API with a Bearer token and a ``phone_number_id``; Meta
pushes inbound messages and delivery receipts to
``/webhooks/messaging/meta/`` as nested, batched JSON signed with the app
secret.

* **Sending** -- POST JSON to
  ``{GRAPH}/{version}/{META_PHONE_NUMBER_ID}/messages`` with
  ``Authorization: Bearer {META_ACCESS_TOKEN}``. Free-form text is
  ``{"messaging_product": "whatsapp", "to": ..., "type": "text", ...}``;
  templates use ``"type": "template"`` with a name, a language code and a
  components array built from ``params``. The response's
  ``messages[0].id`` (``wamid...``) is the provider message id that later
  status webhooks reference.

* **parse_webhook** -- one request carries ``entry[].changes[].value``,
  which holds ``messages[]`` (inbound) and ``statuses[]`` (receipts for our
  own sends), so a single POST can yield many
  :class:`~messaging.providers.types.InboundEvent`. Numbers arrive as bare
  digits and are normalized back to E.164; timestamps are unix seconds.

* **verify_signature** -- ``X-Hub-Signature-256``:
  ``"sha256=" + HMAC_SHA256(META_APP_SECRET, raw body)``, compared in
  constant time against ``request.body`` exactly as received. An unset
  ``META_APP_SECRET`` rejects everything rather than waving traffic through:
  a webhook that anyone on the internet can POST to is the whole attack
  surface of this integration.

* **handshake** -- Meta GETs the webhook once at subscribe time with
  ``hub.mode=subscribe``, ``hub.verify_token`` and ``hub.challenge``. The
  token must equal ``META_VERIFY_TOKEN`` (again, an unset setting fails
  closed) before the challenge is echoed back.

Media caveat: an inbound photo/audio/document arrives as an *id*, not a URL.
Resolving it costs a second authorized Graph call, and what comes back is a
short-lived, token-gated CDN link -- unusable from a browser (the download
needs the Bearer token) and dead within minutes. So the webhook downloads the
bytes right then, while the link is fresh, into Django's default storage
(``MEDIA_ROOT`` locally; swap ``STORAGES`` for object storage without touching
this module) and stores *our* durable URL on the message. Both Graph calls and
the download run inline on the webhook request under tight timeouts and a size
cap, and any failure is logged rather than raised -- a webhook never fails
over an image, it just lands the message without media.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import re
from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.crypto import constant_time_compare

from .base import MessagingProvider
from .types import (
    MEDIA_PLACEHOLDERS,
    AUTH_BUTTON_TEXT_DEFAULT,
    InboundEvent,
    MessageStatus,
    SendOutcomeUnknown,
    TemplateSpec,
    TemplateStatus,
    TemplateVerdict,
)

logger = logging.getLogger(__name__)

#: Pinned Graph API version. Meta keeps each version working for ~2 years and
#: the dashboard's sample calls show the current one; bump deliberately after
#: reading the changelog, never implicitly.
_GRAPH_API_VERSION = "v25.0"
_GRAPH_BASE = "https://graph.facebook.com"

_REQUEST_TIMEOUT = 10
#: Sends get their own (connect, read) budget with a longer read leg: Graph
#: accepts a message quickly but can take several seconds to answer under
#: load, and a timeout here is the worst outcome a send has -- the message
#: went out and the CRM never learned its id (``SendOutcomeUnknown``). Kept
#: under Vercel's 30s function cap with room for the view's own DB writes.
_SEND_TIMEOUT = (5, 20)
#: Media resolution + download happen inline on the webhook request, which
#: Meta expects answered fast -- a tighter budget than a send the user is
#: waiting on. Applied per call (id lookup, then download).
_MEDIA_TIMEOUT = 5

#: Refuse to pull anything bigger into memory on a webhook thread. Covers
#: every sticker/image and most audio/video WhatsApp accepts; an oversized
#: document simply lands without media (logged), the message itself survives.
_MEDIA_MAX_BYTES = 16 * 1024 * 1024

#: Where downloaded media lives inside default storage.
_MEDIA_STORAGE_DIR = "whatsapp"

#: The mimes WhatsApp actually sends, mapped to extensions explicitly --
#: ``mimetypes.guess_extension`` is platform-dependent for some of these
#: (e.g. ``image/jpeg`` -> ``.jpe`` on some systems). Anything else falls
#: back to ``guess_extension``.
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
}

#: Language for templates when ``params`` doesn't override it (see
#: :meth:`MetaProvider.send_template`). The CRM writes to Colombian numbers.
_DEFAULT_TEMPLATE_LANGUAGE = "es"

#: Meta's status vocabulary happens to match ours one-for-one; the map exists
#: so an unknown future value is skipped instead of blowing up the batch.
_STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}

#: Meta's template-approval vocabulary -> ours. PAUSED and DISABLED are
#: approved templates Meta has pulled for quality reasons; what matters to an
#: agent is that a send will bounce, so they read as rechazada with the Meta
#: state named in the reason. Anything not listed is skipped rather than
#: guessed (see ``template_verdicts``).
_TEMPLATE_STATUS_MAP = {
    "APPROVED": TemplateStatus.APPROVED,
    "PENDING": TemplateStatus.PENDING,
    "IN_APPEAL": TemplateStatus.PENDING,
    "REJECTED": TemplateStatus.REJECTED,
    "PAUSED": TemplateStatus.REJECTED,
    "DISABLED": TemplateStatus.REJECTED,
}

#: MessageTemplate.buttons "type" -> Meta's button type and the key the
#: button's target travels under.
_BUTTON_TYPES = {
    "quick_reply": ("QUICK_REPLY", None, None),
    "url": ("URL", "url", "url"),
    "phone": ("PHONE_NUMBER", "phone", "phone_number"),
}

#: Pages of the catalogue listing to follow before giving up. 100 templates a
#: page; a WABA with more than 1000 templates is not this CRM's problem yet.
_TEMPLATE_LIST_PAGE_SIZE = 100
_TEMPLATE_LIST_MAX_PAGES = 10

#: The same webhook carries Messenger and Instagram traffic once those
#: products are added to the app; the top-level ``object`` says which.
_OBJECT_CHANNELS = {
    "whatsapp_business_account": "whatsapp",
    "page": "messenger",
    "instagram": "instagram-dm",
}

#: Message types that carry a media id plus an optional caption. Placeholder
#: bodies for caption-less media live in ``types.MEDIA_PLACEHOLDERS``, shared
#: with the Inbox.
_MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")


def _graph_reason(response) -> str:
    """The human-readable part of a Graph error body, or "" if it has none.

    Meta puts the actionable sentence in ``error.error_user_title`` /
    ``error_user_msg`` when it has copy written for a person ("El nombre de
    la plantilla ya existe"), and in the developer-facing ``error.message``
    otherwise. Anything else -- HTML from a proxy, an empty body -- yields
    "", so the caller falls back to the bare status code.
    """
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return ""
    if not isinstance(error, dict):
        return ""
    title = str(error.get("error_user_title") or "").strip()
    detail = str(error.get("error_user_msg") or error.get("message") or "").strip()
    if title and detail and title != detail:
        return f"{title}: {detail}"
    return title or detail


def _raise_for_graph_status(response, what: str) -> None:
    """``raise_for_status()`` that keeps Meta's explanation.

    ``requests`` renders an HTTPError as "400 Client Error: Bad Request for
    url: ...", which says nothing about *why*, and the reason is in the JSON
    body it discards. That matters here because this string is what the
    person who just pressed "Crear plantilla" reads: the Plantillas page
    shows ``str(exc)`` verbatim (core.views.plantilla_editor). The raw body
    is still logged by the caller; only the actionable sentence travels.
    """
    if response.status_code < 400:
        return
    reason = _graph_reason(response)
    # Meta's own sentence leads when it wrote one -- these strings get read
    # by whoever pressed the button, and the caller's framing is already
    # around them ("...pero WhatsApp no la aceptó para revisión: <this>").
    # ``what`` only has to carry the context when Graph explained nothing.
    message = (
        f"{reason} (HTTP {response.status_code})"
        if reason
        else f"{what}: HTTP {response.status_code}"
    )
    raise requests.HTTPError(message, response=response)


def _to_e164(number: str) -> str:
    """Meta reports numbers as bare digits (``573000000099``); the CRM stores
    E.164. Anything already prefixed is left alone."""
    number = (number or "").strip()
    if not number:
        return ""
    return number if number.startswith("+") else f"+{number}"


def _parse_timestamp(raw) -> datetime | None:
    """Unix seconds (as a string, in Meta's payloads) -> aware datetime."""
    try:
        return datetime.fromtimestamp(int(raw), tz=dt_timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_pricing(raw) -> dict | None:
    """Meta's ``pricing`` object off a status, normalized for the CRM.

    What it is: Meta's own verdict on what a message cost it -- which rate
    bucket applied, never an amount (there is no money anywhere in this
    object). ``services._apply_status_event`` uses it to correct the estimate
    the CRM froze at send time.

    Present only on the ``sent`` status and on one of ``delivered``/``read``,
    so most receipts return ``None`` here. Every field is read defensively:
    Meta's payloads are not consistent about which are included, and
    ``billable`` in particular is on its way out ("use pricing.type and
    pricing.category together" -- Meta's own reference).

    ``category`` is deliberately kept verbatim, including the hyphen in
    ``authentication-international``: this is what Meta billed at, and the
    same value spelled with an underscore appears on the analytics endpoint.
    Normalizing here would hide that mismatch from whoever reconciles later.
    """
    if not isinstance(raw, dict):
        return None
    pricing = {
        "billable": raw.get("billable"),
        "model": raw.get("pricing_model") or "",
        "category": raw.get("category") or "",
        "type": raw.get("type") or "",
    }
    # An object with nothing usable in it is the same as no object at all.
    if not any(value not in (None, "") for value in pricing.values()):
        return None
    return pricing


class MetaProvider(MessagingProvider):
    name = "meta"

    # --- Sending -----------------------------------------------------------

    def send_text(self, to: str, body: str) -> str:
        return self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                # preview_url off: link previews are fetched by Meta and leak
                # that a message was processed before the recipient opens it.
                "text": {"preview_url": False, "body": body},
            }
        )

    def send_image(self, to: str, image_url: str, caption: str = "") -> str:
        """An image by public link -- Meta fetches it, so the URL must be
        reachable from the internet (Vercel Blob is; a local MEDIA_ROOT is
        not, which is fine: the fake provider is what runs locally)."""
        image = {"link": image_url}
        if caption:
            image["caption"] = caption
        return self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "image",
                "image": image,
            }
        )

    def send_template(self, to: str, template_name: str, params: dict) -> str:
        """Send a pre-approved template -- the only way out of the 24h window.

        ``params`` fills the template's body placeholders. Two shapes, picked
        automatically: all-numeric keys (``{"1": "Ana"}``) become positional
        parameters in numeric order; any other key becomes a *named*
        parameter, matching templates authored with ``{{nombre}}``
        placeholders. The reserved key ``_language`` overrides the language
        code for one send, and ``_category`` says whether this is an
        authentication template, whose copy-code button has to be handed the
        code a second time (see below).
        """
        params = dict(params or {})
        language = str(params.pop("_language", _DEFAULT_TEMPLATE_LANGUAGE))
        category = str(params.pop("_category", ""))
        # Meta renders the template itself from its own approved copy; the
        # CRM's pre-rendered body is only for text-only providers.
        params.pop("_rendered", None)

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }
        parameters = self._build_template_parameters(params)
        if parameters:
            components = [{"type": "body", "parameters": parameters}]
            if category == "authentication":
                # The COPY_CODE button carries the code as its own parameter:
                # Meta refuses an authentication send that fills the body and
                # leaves the button empty. Same value, said twice -- the first
                # variable is the code (an authentication template has exactly
                # one, written by Meta).
                components.append(
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [
                            {"type": "text", "text": parameters[0]["text"]}
                        ],
                    }
                )
            payload["template"]["components"] = components
        return self._post_message(payload)

    @staticmethod
    def _build_template_parameters(params: dict) -> list[dict]:
        if not params:
            return []
        if all(str(key).isdigit() for key in params):
            ordered = sorted(params.items(), key=lambda item: int(item[0]))
            return [{"type": "text", "text": str(value)} for _, value in ordered]
        return [
            {"type": "text", "parameter_name": str(key), "text": str(value)}
            for key, value in params.items()
        ]

    def _post_message(self, payload: dict) -> str:
        if not settings.META_ACCESS_TOKEN or not settings.META_PHONE_NUMBER_ID:
            raise RuntimeError(
                "Meta provider is not configured: set META_ACCESS_TOKEN and "
                "META_PHONE_NUMBER_ID (see .env.example)"
            )

        url = (
            f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/"
            f"{settings.META_PHONE_NUMBER_ID}/messages"
        )
        try:
            response = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=_SEND_TIMEOUT,
            )
        except requests.ReadTimeout as exc:
            # The request left and Graph never answered: the message may have
            # been accepted. Not a failure -- see SendOutcomeUnknown. (A
            # ConnectTimeout never reached Graph and stays an ordinary error.)
            logger.warning("meta send unconfirmed: read timeout to=%s", payload.get("to"))
            raise SendOutcomeUnknown(
                f"WhatsApp no respondió en {_SEND_TIMEOUT[1]} segundos"
            ) from exc

        if response.status_code >= 400:
            # Graph puts the actionable part (invalid template, number not in
            # the test allow-list, expired token) in the body, which
            # raise_for_status() throws away. Log it -- never the token.
            logger.error(
                "meta send failed: HTTP %s to=%s body=%s",
                response.status_code,
                payload.get("to"),
                response.text[:500],
            )
        _raise_for_graph_status(response, "WhatsApp rechazó el envío")

        data = response.json()
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"meta send response without a message id: {data!r}") from exc

    # --- Template catalogue --------------------------------------------------
    #
    # Templates are objects on the WhatsApp Business Account, so these calls
    # address META_WABA_ID, not the phone number. Both directions are here:
    # ``create_template`` submits a plantilla for review and
    # ``template_verdicts`` reads the review results back.

    def create_template(self, spec: TemplateSpec) -> str | None:
        """POST ``/{waba_id}/message_templates`` and return Meta's template id.

        The components array mirrors the editor's sections one-for-one:
        header, body, footer, buttons. Two Meta requirements shape it. A body
        with ``{{n}}`` variables must ship one example per variable (the
        editor already forces those samples), and a media header must ship
        a *handle* to sample bytes, obtained through the Resumable Upload
        API first -- a public URL is not accepted (see ``_header_handle``).
        """
        self._require_catalogue()

        payload = {
            "name": spec.name,
            "language": spec.language,
            "category": spec.category.upper(),
            "components": self._build_template_components(spec),
        }
        url = f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{settings.META_WABA_ID}/message_templates"
        response = requests.post(
            url, json=payload, headers=self._bearer_headers(), timeout=_REQUEST_TIMEOUT
        )
        if response.status_code >= 400:
            # Graph's body says *why* (name taken, a {{2}} without an example,
            # a URL button pointing at a blocked domain) -- keep it. Never the
            # token.
            logger.error(
                "meta create_template failed: HTTP %s name=%s body=%s",
                response.status_code,
                spec.name,
                response.text[:500],
            )
        _raise_for_graph_status(response, "WhatsApp no aceptó la plantilla")

        data = response.json()
        template_id = data.get("id")
        if not template_id:
            raise ValueError(f"meta create_template response without an id: {data!r}")
        return str(template_id)

    def _build_template_components(self, spec: TemplateSpec) -> list[dict]:
        if spec.category == "authentication":
            return self._build_authentication_components(spec)

        components: list[dict] = []

        if spec.header_type == "text":
            components.append({"type": "HEADER", "format": "TEXT", "text": spec.header_text})
        elif spec.header_type in ("image", "video", "document"):
            components.append(
                {
                    "type": "HEADER",
                    "format": spec.header_type.upper(),
                    "example": {"header_handle": [self._header_handle(spec)]},
                }
            )

        body: dict = {"type": "BODY", "text": spec.body}
        if spec.body_sample_values:
            # One row of examples -- Meta accepts several, one is required.
            body["example"] = {"body_text": [list(spec.body_sample_values)]}
        components.append(body)

        if spec.footer:
            components.append({"type": "FOOTER", "text": spec.footer})

        buttons = []
        for button in spec.buttons:
            kind = _BUTTON_TYPES.get(button.get("type", ""))
            if kind is None:
                continue
            meta_type, our_key, meta_key = kind
            entry = {"type": meta_type, "text": button.get("text", "")}
            if our_key:
                entry[meta_key] = button.get(our_key, "")
            buttons.append(entry)
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})

        return components

    @staticmethod
    def _build_authentication_components(spec: TemplateSpec) -> list[dict]:
        """The fixed shape Meta requires of an AUTHENTICATION template.

        None of the editor's copy fields appear here, and that is the point:
        Meta writes and localizes an authentication template's text itself
        ("Tu código de verificación es {{1}}"), and refuses one that ships
        its own ``BODY`` ``text`` -- which is exactly what this method used
        to send, so every plantilla created under Autenticación was rejected
        at submission. All Meta takes are the three knobs the editor's
        Autenticación panel collects, and the OTP button, which is required.

        ``COPY_CODE`` is the only OTP type offered: ``ONE_TAP`` and
        ``ZERO_TAP`` need an Android package name and signing hash, which
        this CRM has nowhere to put and no app to put there.
        """
        body: dict = {"type": "BODY"}
        if spec.auth_security_recommendation:
            body["add_security_recommendation"] = True

        components: list[dict] = [body]
        if spec.auth_code_expiration_minutes:
            components.append(
                {
                    "type": "FOOTER",
                    "code_expiration_minutes": spec.auth_code_expiration_minutes,
                }
            )
        components.append(
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "OTP",
                        "otp_type": "COPY_CODE",
                        "text": spec.auth_button_text or AUTH_BUTTON_TEXT_DEFAULT,
                    }
                ],
            }
        )
        return components

    def _header_handle(self, spec: TemplateSpec) -> str:
        """Push the header's sample file through the Resumable Upload API and
        return the handle Meta wants in ``example.header_handle``.

        Two calls: open an upload session on the *app* (``/{app_id}/uploads``
        with the file's length and type), then POST the bytes to that session.
        The whole file goes in one request -- these are template samples,
        capped at 16 MB by the editor, not a video pipeline.
        """
        if not settings.META_APP_ID:
            raise RuntimeError(
                "Meta template with a media header needs META_APP_ID for the "
                "Resumable Upload API (see .env.example)"
            )
        media = spec.header_media
        if media is None:
            raise ValueError(f"template {spec.name!r} has a media header but no file")

        media.open("rb")
        try:
            data = media.read()
        finally:
            media.close()
        file_name = (getattr(media, "name", "") or "sample").rsplit("/", 1)[-1]
        file_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        session = requests.post(
            f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{settings.META_APP_ID}/uploads",
            params={
                "file_name": file_name,
                "file_length": len(data),
                "file_type": file_type,
            },
            headers=self._bearer_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        if session.status_code >= 400:
            logger.error(
                "meta upload session failed: HTTP %s body=%s",
                session.status_code,
                session.text[:500],
            )
        _raise_for_graph_status(
            session, "WhatsApp no aceptó el archivo de la cabecera"
        )
        session_id = session.json().get("id")
        if not session_id:
            raise ValueError(f"meta upload session without an id: {session.text[:200]}")

        upload = requests.post(
            f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{session_id}",
            data=data,
            headers={
                # This endpoint wants the OAuth scheme, not Bearer -- Meta's
                # one inconsistency here, documented as such.
                "Authorization": f"OAuth {settings.META_ACCESS_TOKEN}",
                "file_offset": "0",
                "Content-Type": file_type,
            },
            timeout=_REQUEST_TIMEOUT * 3,
        )
        if upload.status_code >= 400:
            logger.error(
                "meta upload failed: HTTP %s body=%s", upload.status_code, upload.text[:500]
            )
        _raise_for_graph_status(
            upload, "WhatsApp no aceptó el archivo de la cabecera"
        )
        handle = upload.json().get("h")
        if not handle:
            raise ValueError(f"meta upload without a handle: {upload.text[:200]}")
        return handle

    def template_verdicts(self) -> list[TemplateVerdict]:
        """GET the catalogue and normalize every entry with a status we
        understand. Follows ``paging.next`` so a long catalogue comes back
        whole (up to a sane page cap)."""
        self._require_catalogue()

        url = f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{settings.META_WABA_ID}/message_templates"
        params: dict | None = {
            "fields": "id,name,language,status,rejected_reason",
            "limit": _TEMPLATE_LIST_PAGE_SIZE,
        }
        verdicts: list[TemplateVerdict] = []
        for _ in range(_TEMPLATE_LIST_MAX_PAGES):
            response = requests.get(
                url, params=params, headers=self._bearer_headers(), timeout=_REQUEST_TIMEOUT
            )
            if response.status_code >= 400:
                logger.error(
                    "meta template list failed: HTTP %s body=%s",
                    response.status_code,
                    response.text[:500],
                )
            _raise_for_graph_status(
                response, "WhatsApp no devolvió el catálogo de plantillas"
            )
            data = response.json()

            for entry in data.get("data", []):
                status = _TEMPLATE_STATUS_MAP.get(str(entry.get("status", "")).upper())
                if status is None or not entry.get("name"):
                    continue
                reason = str(entry.get("rejected_reason") or "")
                if reason.upper() == "NONE":
                    reason = ""
                # A paused/disabled template has no rejected_reason -- the
                # Meta state itself is the explanation worth showing.
                if status is TemplateStatus.REJECTED and not reason:
                    meta_state = str(entry.get("status", "")).upper()
                    if meta_state != "REJECTED":
                        reason = meta_state
                verdicts.append(
                    TemplateVerdict(
                        name=str(entry["name"]),
                        language=str(entry.get("language") or _DEFAULT_TEMPLATE_LANGUAGE),
                        status=status,
                        rejection_reason=reason,
                        provider_template_id=str(entry.get("id") or ""),
                    )
                )

            next_url = (data.get("paging") or {}).get("next")
            if not next_url:
                break
            # ``next`` is a complete URL with the cursor and fields baked in.
            url, params = next_url, None
        return verdicts

    @staticmethod
    def _require_catalogue() -> None:
        if not settings.META_ACCESS_TOKEN or not settings.META_WABA_ID:
            raise RuntimeError(
                "Meta template catalogue is not configured: set META_ACCESS_TOKEN "
                "and META_WABA_ID (see .env.example)"
            )

    @staticmethod
    def _bearer_headers() -> dict:
        return {
            "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

    # --- Webhook -----------------------------------------------------------

    def verify_signature(self, request) -> bool:
        secret = settings.META_APP_SECRET
        if not secret:
            logger.error(
                "META_APP_SECRET is not set -- rejecting the Meta webhook. "
                "Without it any caller could post messages into the CRM."
            )
            return False

        header = request.headers.get("X-Hub-Signature-256", "")
        if not header.startswith("sha256="):
            return False

        expected = hmac.new(
            secret.encode("utf-8"), request.body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(header[len("sha256="):], expected)

    def handshake(self, request) -> str | None:
        """Meta's one-off subscribe check: echo ``hub.challenge`` only when
        ``hub.verify_token`` matches ours."""
        token = settings.META_VERIFY_TOKEN
        if not token:
            logger.error("META_VERIFY_TOKEN is not set -- refusing the handshake")
            return None
        if request.GET.get("hub.mode") != "subscribe":
            return None
        if not constant_time_compare(request.GET.get("hub.verify_token", ""), token):
            logger.warning("meta handshake rejected: verify token mismatch")
            return None
        return request.GET.get("hub.challenge")

    def parse_webhook(self, request) -> list[InboundEvent]:
        """Flatten ``entry[].changes[].value`` into a list of events.

        Meta batches: one POST can carry several entries, each with inbound
        messages *and* delivery receipts. Anything unrecognized inside a
        batch is skipped with a log line rather than failing the whole
        request -- the rest of the batch is still real traffic.
        """
        try:
            payload = json.loads(request.body)
            entries = payload["entry"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"unparseable meta webhook payload: {exc}") from exc
        if not isinstance(entries, list):
            raise ValueError("meta webhook 'entry' is not a list")

        channel = _OBJECT_CHANNELS.get(payload.get("object", ""), "whatsapp")
        events: list[InboundEvent] = []

        for entry in entries:
            for change in (entry or {}).get("changes", []):
                value = (change or {}).get("value", {}) or {}
                our_number = _to_e164(
                    (value.get("metadata") or {}).get("display_phone_number", "")
                )
                # contacts[] names the sender; keyed by wa_id so a batch with
                # several senders attributes each name to the right message.
                names = {
                    contact.get("wa_id", ""): (contact.get("profile") or {}).get("name", "")
                    for contact in value.get("contacts", []) or []
                }

                for raw in value.get("messages", []) or []:
                    event = self._parse_message(raw, names, our_number, channel)
                    if event is not None:
                        events.append(event)

                for raw in value.get("statuses", []) or []:
                    event = self._parse_status(raw, channel)
                    if event is not None:
                        events.append(event)

        return events

    def _parse_message(
        self, raw: dict, names: dict, our_number: str, channel: str
    ) -> InboundEvent | None:
        message_id = (raw or {}).get("id")
        if not message_id:
            logger.warning("meta webhook: message without an id, skipped")
            return None

        sender = raw.get("from", "")
        message_type = raw.get("type", "")
        body = ""
        media_url = ""
        media_type = ""

        if message_type == "text":
            body = (raw.get("text") or {}).get("body", "")
        elif message_type in _MEDIA_TYPES:
            media = raw.get(message_type) or {}
            body = media.get("caption", "") or MEDIA_PLACEHOLDERS.get(message_type, "")
            media_url = self._fetch_and_store_media(media.get("id", ""))
            media_type = message_type if media_url else ""
        elif message_type == "button":
            body = (raw.get("button") or {}).get("text", "")
        elif message_type == "interactive":
            interactive = raw.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            body = reply.get("title", "")
        elif message_type == "location":
            location = raw.get("location") or {}
            body = location.get("name") or (
                f"[ubicación] {location.get('latitude')}, {location.get('longitude')}"
            )
        else:
            # Contacts, orders, system messages, reactions: recorded so the
            # thread doesn't silently lose a turn, but not interpreted.
            logger.info("meta webhook: unhandled message type %r", message_type)
            body = f"[{message_type or 'mensaje no soportado'}]"

        return InboundEvent(
            event_type="message",
            provider_message_id=message_id,
            from_number=_to_e164(sender),
            to_number=our_number,
            body=body,
            media_url=media_url,
            media_type=media_type,
            timestamp=_parse_timestamp(raw.get("timestamp")),
            channel=channel,
            contact_name=names.get(sender, ""),
        )

    def _parse_status(self, raw: dict, channel: str) -> InboundEvent | None:
        message_id = (raw or {}).get("id")
        status = _STATUS_MAP.get(raw.get("status", ""))
        if not message_id or status is None:
            logger.info("meta webhook: unusable status %r, skipped", raw)
            return None

        if status is MessageStatus.FAILED:
            # Why it failed only ever appears here; without this line a failed
            # send is a red tick in the UI with no explanation anywhere.
            logger.warning(
                "meta reported a failed message id=%s errors=%s",
                message_id,
                raw.get("errors"),
            )

        return InboundEvent(
            event_type="status",
            provider_message_id=message_id,
            from_number="",
            to_number=_to_e164(raw.get("recipient_id", "")),
            timestamp=_parse_timestamp(raw.get("timestamp")),
            status=status,
            channel=channel,
            pricing=_parse_pricing(raw.get("pricing")),
        )

    # --- Billing analytics -------------------------------------------------

    def fetch_account(self) -> dict:
        """The WABA's own billing-relevant fields.

        ``currency`` matters because :meth:`fetch_pricing_analytics` reports
        a bare ``cost`` number with no currency attached -- this is what it
        is denominated in. ``is_shared_with_partners`` and ``ownership_type``
        matter because Meta *withholds* cost entirely from an account billed
        through a solution partner's credit line, so a report of zero there
        means "not visible", not "nothing spent". ``timezone_id`` is the
        timezone analytics buckets and monthly tier resets follow.
        """
        if not settings.META_ACCESS_TOKEN or not settings.META_WABA_ID:
            raise RuntimeError(
                "Meta analytics are not configured: set META_ACCESS_TOKEN and "
                "META_WABA_ID (see .env.example)"
            )
        response = requests.get(
            f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{settings.META_WABA_ID}",
            params={
                "fields": "id,name,currency,timezone_id,ownership_type,"
                          "is_shared_with_partners"
            },
            headers={"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"},
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def fetch_pricing_analytics(
        self, start, end, granularity: str = "DAILY", dimensions=("PRICING_CATEGORY", "PRICING_TYPE")
    ) -> list[dict]:
        """What Meta says the account was charged, bucket by bucket.

        ``GET /{WABA_ID}?fields=pricing_analytics.start(..).end(..)...`` --
        a *field expression*, not a normal edge: the parameters go inside the
        field name rather than in the query string, which is why this builds
        a string instead of passing a params dict.

        ``start``/``end`` are datetimes (converted to the unix seconds Meta
        wants). Each returned data point carries ``volume`` (messages
        delivered) and ``cost``, plus whichever of country/phone/
        pricing_category/pricing_type/tier were asked for in ``dimensions``.

        Two things to know about ``cost``. It is described by Meta as
        *approximate* -- the invoice is the record of truth -- and it is
        absent entirely when the account rides a solution partner's credit
        line, in which case every point comes back without it. Callers must
        treat a missing cost as unknown rather than as zero.
        """
        if not settings.META_ACCESS_TOKEN or not settings.META_WABA_ID:
            raise RuntimeError(
                "Meta analytics are not configured: set META_ACCESS_TOKEN and "
                "META_WABA_ID (see .env.example)"
            )

        field = (
            f"pricing_analytics.start({int(start.timestamp())})"
            f".end({int(end.timestamp())})"
            f".granularity({granularity})"
            f".metric_types(COST,VOLUME)"
        )
        if dimensions:
            field += f".dimensions({','.join(dimensions)})"

        url = f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{settings.META_WABA_ID}"
        params = {"fields": field}
        headers = {"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"}

        points: list[dict] = []
        while url:
            response = requests.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
            analytics = payload.get("pricing_analytics") or {}
            # The envelope nests one level deeper than most edges: `data` is
            # a list of result groups, each with its own `data_points`.
            for group in analytics.get("data") or []:
                points.extend(group.get("data_points") or [])
            # Paging lives on the field, not the node, and may be absent.
            url = ((analytics.get("paging") or {}).get("next")) or ""
            params = None
        return points

    def _fetch_and_store_media(self, media_id: str) -> str:
        """Trade a media id for a durable URL of our own.

        Meta's CDN link is token-gated and expires within minutes -- useless
        to the browser and to history. So: resolve the id, pull the bytes down
        while the link is fresh, save them into default storage and return
        *that* URL. Idempotent per media id: a webhook retry finds the
        already-saved file instead of downloading again.

        Fails soft: a webhook must not 500 -- or hang -- because a download
        went wrong. Returning "" lands the message without media.
        """
        if not media_id or not settings.META_ACCESS_TOKEN:
            return ""
        headers = {"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"}
        try:
            response = requests.get(
                f"{_GRAPH_BASE}/{_GRAPH_API_VERSION}/{media_id}",
                headers=headers,
                timeout=_MEDIA_TIMEOUT,
            )
            response.raise_for_status()
            info = response.json()
            cdn_url = info.get("url", "")
            if not cdn_url:
                return ""

            name = self._storage_name(media_id, info.get("mime_type", ""))
            if default_storage.exists(name):
                return default_storage.url(name)

            if int(info.get("file_size") or 0) > _MEDIA_MAX_BYTES:
                logger.warning(
                    "meta: media %s reports %s bytes, over the cap -- not stored",
                    media_id,
                    info.get("file_size"),
                )
                return ""

            data = self._download(cdn_url, headers, media_id)
            if data is None:
                return ""
            return default_storage.url(default_storage.save(name, ContentFile(data)))
        except (requests.RequestException, ValueError, OSError):
            logger.exception("meta: could not fetch media id %s", media_id)
            return ""

    @staticmethod
    def _download(url: str, headers: dict, media_id: str) -> bytes | None:
        """The CDN download itself, streamed so an oversized (or lying about
        its ``file_size``) file is abandoned at the cap rather than read whole
        into a webhook thread's memory."""
        response = requests.get(
            url, headers=headers, timeout=_MEDIA_TIMEOUT, stream=True
        )
        with response:
            response.raise_for_status()
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total > _MEDIA_MAX_BYTES:
                    logger.warning(
                        "meta: media %s exceeded the size cap mid-download", media_id
                    )
                    return None
                chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _storage_name(media_id: str, mime_type: str) -> str:
        """Deterministic storage path for a media id, so retries can find the
        file, with an extension the browser can trust for Content-Type."""
        mime = (mime_type or "").split(";")[0].strip().lower()
        extension = _MIME_EXTENSIONS.get(mime) or mimetypes.guess_extension(mime) or ""
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", media_id)
        return f"{_MEDIA_STORAGE_DIR}/{safe_id}{extension}"
