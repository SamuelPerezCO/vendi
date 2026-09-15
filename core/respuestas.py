"""The rules behind Configuración de mensajería > Respuestas rápidas: what a
quick reply needs to be saveable, and how the composer sends one.

Same stance as :mod:`core.clientes` and :mod:`core.plantillas`: validation is
domain knowledge, not request handling, so it lives here and the view only
shuffles state between the form and these functions.
"""

from __future__ import annotations

from django.conf import settings

from .models import QuickReply

TITLE_MAX = 80
BODY_MAX = 1024

#: What the upload field accepts. WhatsApp itself takes JPEG/PNG (WebP only
#: as stickers), and Meta rejects anything else with an error the agent never
#: sees -- so the check happens here, at save time, with a message.
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
IMAGE_MAX_BYTES = 5 * 1024 * 1024


def form_state(post=None, reply=None) -> dict:
    """The dialog's field values: what was posted, else an existing reply's,
    else blanks. One dict feeds the template and :func:`validate`."""
    if post is not None:
        return {
            "title": (post.get("title") or "").strip(),
            "body": (post.get("body") or "").strip(),
            "is_active": post.get("is_active") == "1",
            "remove_image": post.get("remove_image") == "1",
        }
    if reply is not None:
        return {
            "title": reply.title,
            "body": reply.body,
            "is_active": reply.is_active,
            "remove_image": False,
        }
    return {"title": "", "body": "", "is_active": True, "remove_image": False}


def validate(state: dict, upload=None, reply=None) -> dict:
    """Field name -> Spanish error message; empty when saveable.

    ``upload`` is the request's new image file (or None); ``reply`` the row
    being edited, whose stored image counts as "has an image" unless the
    state asks to remove it. A quick reply must carry *something* to send:
    text, an image, or both.
    """
    errors: dict[str, str] = {}

    if not state["title"]:
        errors["title"] = "Ponle un título para encontrarla en el selector."
    elif len(state["title"]) > TITLE_MAX:
        errors["title"] = f"Máximo {TITLE_MAX} caracteres."

    if len(state["body"]) > BODY_MAX:
        errors["body"] = f"Máximo {BODY_MAX} caracteres."

    if upload is not None:
        content_type = getattr(upload, "content_type", "") or ""
        if content_type not in IMAGE_CONTENT_TYPES:
            errors["image"] = "Sube una imagen JPG, PNG o WebP."
        elif upload.size > IMAGE_MAX_BYTES:
            errors["image"] = "La imagen no puede pesar más de 5 MB."

    keeps_image = bool(reply and reply.image) and not state["remove_image"]
    has_image = (upload is not None and "image" not in errors) or keeps_image
    if not state["body"] and not has_image:
        errors["body"] = "Escribe el texto o adjunta una imagen: algo hay que enviar."

    return errors


def apply(state: dict, upload=None, reply=None, user=None) -> QuickReply:
    """Create or update the quick reply from a state :func:`validate` accepted.

    Detaching an image (removing or replacing it) leaves the stored *file*
    alone: every message already sent with it holds that URL in
    ``Message.media_url``, and deleting the bytes would turn real history
    into broken images in the customer's thread. Same stance the rest of the
    app takes -- tags archive, users deactivate, history is never rewritten.
    The cost is an orphaned file per replacement, which is the cheap side of
    the trade.
    """
    reply = reply or QuickReply(
        created_by=user if getattr(user, "is_authenticated", False) else None
    )
    reply.title = state["title"]
    reply.body = state["body"]
    reply.is_active = state["is_active"]
    if state["remove_image"] and reply.image:
        reply.image = ""
    if upload is not None:
        reply.image = upload
    reply.save()
    return reply


def image_url(reply: QuickReply, request=None) -> str:
    """The absolute URL a provider is handed for the reply's image, or "".

    Meta fetches this link from its own servers and rejects anything that
    isn't absolute -- "(#100) Param image.link is not a valid URI" -- so a
    relative path here means the photo never reaches the customer, and the
    only sign is a 400 in the logs after the agent has already clicked send.

    What storage answers varies: Vercel Blob gives an absolute CDN URL and
    needs nothing done to it, while the local filesystem and
    ``core.storage.DatabaseStorage`` both give a path rooted at "/". Those
    get an origin attached -- and *which* origin is the whole point:

    The link's reader is Meta, not the agent. Vercel's deployment protection
    puts every alias of this project behind SSO except the production domain,
    and a signed-in teammate never notices, because their browser session
    walks straight through. So building the link from the request's Host --
    whichever alias the agent happened to be logged in through -- handed Meta
    a URL that answered with a redirect to vercel.com/sso-api instead of a
    JPEG. The send looked fine (Meta accepted it and returned an id), and the
    message went to ``failed`` a few seconds later when Meta's fetch hit the
    login page. Four sends in a row went that way from the team alias while
    two from the production domain delivered, same reply, same file.

    Hence the order here: ``settings.PUBLIC_BASE_URL`` -- the one public
    origin -- first, always. The request is the fallback for a deployment
    with no public origin configured at all (local development, where Meta
    could not reach the link anyway).
    """
    if not reply.image:
        return ""
    url = reply.image.url
    if url.startswith(("http://", "https://")):
        return url
    base = getattr(settings, "PUBLIC_BASE_URL", "")
    if base:
        return f"{base}{url}"
    if request is not None:
        return request.build_absolute_uri(url)
    # Without an origin the link is unusable to Meta. Returning it anyway
    # would send a message whose image silently never renders; returning ""
    # sends the caption alone, which at least arrives.
    return ""
