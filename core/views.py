"""
Views for the single-page shell.

:func:`section` serves all 14 sidebar destinations. It answers two ways:

* a normal browser request  -> full page (base.html + sidebar + section)
* an HTMX request           -> just the section fragment, swapped into #content

That dual response is what makes the shell work without full page reloads while
keeping every URL directly linkable, bookmarkable and back-button friendly.

Sections that need their own data register a context builder in
:data:`SECTION_CONTEXT` rather than adding branches to ``section()``.

:func:`welcome` serves the root URL and answers the same two ways, but is not a
section: it is the shell at rest, with no sidebar icon selected.
"""

import posixpath

from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render
from django.template import TemplateDoesNotExist
from django.template.loader import get_template, render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.utils.http import url_has_allowed_host_and_scheme

import json

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth import update_session_auth_hash
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q

from messaging import pricing
from messaging import services as messaging_services
from messaging.models import Conversation, Tag
from messaging.providers.base import MessagingProvider as MessagingProviderBase

from . import (
    agents,
    automatizaciones,
    calendario,
    clientes,
    comercio,
    crm,
    embudos,
    estadisticas,
    estadisticas_atribuciones,
    estadisticas_embudos,
    estadisticas_periodos,
    estadisticas_temas,
    estadisticas_tiempos,
    estadisticas_ventas,
    estadisticas_volumen,
    inbox,
    mensajeria,
    plantillas,
    respuestas,
    xlsx,
)
from .middleware import SESSION_KEY
from .models import (
    CalendarEvent,
    Client,
    ClientList,
    MessageTemplate,
    MessagingSettings,
    QuickReply,
)
from .nav import (
    DEFAULT_SECTION,
    NAV_BY_KEY,
    PRIMARY_NAV,
    SECONDARY_NAV,
    WELCOME_SHORTCUTS,
)

#: Rendered when a section has no template of its own yet.
PLACEHOLDER_TEMPLATE = "sections/_placeholder.html"


def _section_template(key: str) -> str:
    """Return ``sections/<key>.html`` if it exists, else the placeholder.

    This is the extension point: to build out a section, create the file. No
    view, URL or nav change is needed -- it is picked up on the next request.
    """
    candidate = f"sections/{key}.html"
    try:
        get_template(candidate)
    except TemplateDoesNotExist:
        return PLACEHOLDER_TEMPLATE
    return candidate


def _is_htmx(request) -> bool:
    """True when HTMX issued the request and wants a fragment back."""
    return request.headers.get("HX-Request") == "true"


# --- Per-section context ---------------------------------------------------


def _thread_context(conversation) -> dict:
    """Context for the chat panel + details panel of one open conversation.

    Shared by the full-page render (``?chat=``), the HTMX chat swap and the
    thread poll, so all three always agree on what a thread looks like.
    """
    window_open = conversation.is_within_24h_window
    return {
        "active_conversation": conversation,
        "active_conversation_id": conversation.pk,
        "chat_messages": conversation.messages.all(),
        "window_open": window_open,
        # Options for the header's assignment dropdown -- everyone configured
        # in APP_AGENTS, whether or not they have logged in yet.
        "assign_options": agents.assignment_options(conversation),
        # A closed window swaps the composer for the plantilla picker, which
        # needs the list; an open one renders nothing from it.
        "template_options": [] if window_open else _template_options(),
    }


def _mark_read(conversation) -> None:
    """Viewing a thread clears its unread badge."""
    if conversation.unread_count:
        conversation.unread_count = 0
        conversation.save(update_fields=["unread_count"])


def _selected_tag_ids(data) -> list[int]:
    """The tag filter's selected ids out of a GET/POST QueryDict, ignoring
    anything non-numeric (hand-edited URLs shouldn't 500 the list)."""
    ids = []
    for raw in data.getlist("tags"):
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    return ids


def _inbox_context(request) -> dict:
    """Extra context for the Inbox screen.

    ``?filter=`` selects the active row in the nav panel, ``?chat=`` the open
    conversation and ``?tags=`` (repeatable) the tag filter. Unknown values
    fall back to the default / no selection rather than 404-ing, so a stale
    bookmark still opens.
    """
    filter_key = request.GET.get("filter", inbox.DEFAULT_FILTER)
    if filter_key not in inbox.FILTER_BY_KEY:
        filter_key = inbox.DEFAULT_FILTER

    tag_ids = _selected_tag_ids(request.GET)

    context = {
        "filter_groups": inbox.FILTER_GROUPS,
        "active_filter": filter_key,
        "counts": inbox.get_counts(),
        "conversations": inbox.get_conversations(filter_key, request.user, tag_ids),
        # The tag-filter dropdown in column 3's toolbar.
        "all_tags": Tag.objects.filter(is_archived=False),
        "selected_tag_ids": tag_ids,
        "active_conversation": None,
        "active_conversation_id": None,
    }

    try:
        chat_id = int(request.GET.get("chat", ""))
    except ValueError:
        chat_id = None
    if chat_id is not None:
        conversation = (
            Conversation.objects.select_related("contact", "assigned_to")
            .filter(pk=chat_id)
            .first()
        )
        if conversation is not None:
            _mark_read(conversation)
            context.update(_thread_context(conversation))

    # ?nuevo=<client id> opens the Nuevo chat modal on that client -- the CRM
    # client card links here. The id is only carried through; the modal
    # body itself is fetched (see nav_panel.html), so an unknown id just
    # opens an unselected picker.
    try:
        context["new_chat_client_id"] = int(request.GET.get("nuevo", ""))
    except ValueError:
        context["new_chat_client_id"] = None

    return context


#: How many clients per page in the CRM table.
CLIENTS_PER_PAGE = 25


def _table_param(request, name: str) -> str:
    """One of the client table's view parameters (``q``, ``page``).

    Read from the query string normally, and from the body on the mutation
    POSTs -- the dialogs carry the current search and page as hidden fields so
    a save re-renders the table the agent was actually looking at, rather than
    bouncing them back to an unfiltered page 1.
    """
    return (request.GET.get(name) or request.POST.get(name) or "").strip()


def _clientes_context(request) -> dict:
    """Paginated (and optionally searched) client list for the Clientes table,
    plus the option lists its create/edit dialog renders from."""
    query = _table_param(request, "q")
    clients = Client.objects.all()
    if query:
        # Phone matching ignores formatting: "316 768" finds +573167687288.
        digits = "".join(char for char in query if char.isdigit())
        matches = (
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
        if digits:
            matches |= Q(phone__contains=digits)
        else:
            matches |= Q(phone__icontains=query)
        clients = clients.filter(matches)

    page = Paginator(clients, CLIENTS_PER_PAGE).get_page(_table_param(request, "page"))
    return {
        "clients": page,
        "page_obj": page,
        "client_query": query,
        "countries": clientes.COUNTRIES,
        "channels": Client.CHANNEL_CHOICES,
    }


def _lista_clientes_context(request) -> dict:
    """Client lists for the Lista de clientes table.

    The contact count is annotated in one query rather than counted per row in
    the template; the template renders it as ``contact_count``.
    """
    return {
        "client_lists": ClientList.objects.annotate(contact_count=Count("clients")),
    }


def _etiquetas_context(request) -> dict:
    """Tags for the Etiquetas admin table, with usage counts.

    Usage is annotated in one query (never counted per row in the template),
    and archived tags sink below the active ones instead of disappearing --
    they still label old conversations, so they remain visible and restorable.
    """
    return {
        "tags": (
            Tag.objects.select_related("created_by")
            .annotate(usage=Count("conversation_tags"))
            .order_by("is_archived", "name")
        ),
        "tag_colors": Tag.COLOR_CHOICES,
    }


def _mi_calendario_context(request) -> dict:
    """Data for the Mi calendario panel: the sidebar preferences (session-
    kept until real users/auth exist) and the event modal's option lists."""
    return {
        "calendar_prefs": calendario.get_prefs(request.session),
        "event_types": calendario.EVENT_TYPES,
        "slot_choices": calendario.SLOT_CHOICES,
        "reminder_choices": calendario.REMINDER_CHOICES,
        "contacts": Client.objects.all(),
        "advisors": get_user_model().objects.filter(is_active=True).order_by("username"),
    }


#: Column headers of the clients export, in sheet order. The row builder in
#: clientes_export must follow this order.
CLIENT_EXPORT_COLUMNS = [
    "Nombres", "Apellidos", "Teléfono", "País", "Mail", "Canal",
    "Cliente desde", "Conversaciones", "Etiquetas",
]


def _exportaciones_context(request) -> dict:
    """What the Exportaciones page shows before the download: how many rows
    it will hold and which columns -- from the same queryset the export uses."""
    return {
        "client_count": Client.objects.count(),
        "export_columns": CLIENT_EXPORT_COLUMNS,
    }


def _usuarios_context(request) -> dict:
    """The team: every active agent, plus the deactivated ones so a master
    can restore them. ``can_manage`` is what shows the create/edit controls
    -- the page is read-only for everyone else."""
    User = get_user_model()
    active = agents.agent_users()
    active_ids = {user.pk for user in active}
    inactive = [
        user for user in User.objects.filter(is_active=False).order_by("first_name", "username")
        if agents.is_app_user(user)
    ]
    # One query for the whole Maestros group instead of is_master() per row.
    master_ids = set(
        User.objects.filter(groups__name=agents.MASTER_GROUP).values_list("pk", flat=True)
    )
    return {
        "team": [
            {
                "user": user,
                "is_master": user.is_superuser or user.pk in master_ids,
            }
            for user in active + inactive
        ],
        "can_manage": agents.is_master(request.user),
        "active_ids": active_ids,
    }


#: CRM view key -> callable(request) -> dict. Panels without an entry need no data.
PANEL_CONTEXT = {
    "clientes": _clientes_context,
    "etiquetas": _etiquetas_context,
    "lista-clientes": _lista_clientes_context,
    "mi-calendario": _mi_calendario_context,
    "exportaciones": _exportaciones_context,
    "usuarios": _usuarios_context,
}


def _crm_context(request) -> dict:
    """Extra context for the screens that mount the account nav (CRM, Campañas).

    Both sections render the same core.crm sections and panels; only the
    section template differs (see sections/crm.html and sections/campanas.html).

    ``?view=`` selects the active row in the secondary nav. An unknown value
    falls back to the default rather than 404-ing, so a stale bookmark opens.
    """
    view_key = request.GET.get("view", crm.DEFAULT_VIEW)
    if view_key not in crm.VIEW_BY_KEY:
        view_key = crm.DEFAULT_VIEW

    context = {
        "crm_sections": crm.SECTIONS,
        "active_view": view_key,
        "crm_view": crm.VIEW_BY_KEY[view_key],
        "panel_template": crm.panel_template(view_key),
    }
    builder = PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))
    return context


def _embudos_panel_context(request) -> dict:
    """Data for the Embudos panel.

    ``funnels`` is what the panel branches on: empty renders the empty-state
    card, non-empty is where the funnel list will go.
    """
    return {"funnels": embudos.get_funnels()}


#: Embudos view key -> callable(request) -> dict. Views without an entry need no data.
EMBUDOS_PANEL_CONTEXT = {
    "embudos": _embudos_panel_context,
}


def _embudos_context(request) -> dict:
    """Extra context for the Embudos screen.

    ``?view=`` selects the active row in the secondary nav. An unknown value
    falls back to the default rather than 404-ing, so a stale bookmark opens.
    """
    view_key = request.GET.get("view", embudos.DEFAULT_VIEW)
    if view_key not in embudos.VIEW_BY_KEY:
        view_key = embudos.DEFAULT_VIEW

    context = {
        "embudos_nav": embudos.VIEWS,
        "active_view": view_key,
        "embudos_view": embudos.VIEW_BY_KEY[view_key],
        "panel_template": embudos.panel_template(view_key),
    }
    builder = EMBUDOS_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))
    return context


def _automatizaciones_panel_context(request) -> dict:
    """Data for the Chatbots de flujo (Flujos) panel.

    ``flows`` is what the panel branches on: empty renders the "Sin flujos"
    state, non-empty is where the flow list will go.
    """
    return {"flows": automatizaciones.get_flows()}


#: Automatizaciones view key -> callable(request) -> dict. Views without an
#: entry need no data.
AUTOM_PANEL_CONTEXT = {
    "chatbots-flujo": _automatizaciones_panel_context,
}


def _automatizaciones_context(request) -> dict:
    """Extra context for the Automatizaciones screen.

    ``?view=`` selects the active row in the secondary nav. An unknown value
    falls back to the default rather than 404-ing, so a stale bookmark opens.
    """
    view_key = request.GET.get("view", automatizaciones.DEFAULT_VIEW)
    if view_key not in automatizaciones.VIEW_BY_KEY:
        view_key = automatizaciones.DEFAULT_VIEW

    context = {
        "autom_nav": automatizaciones.VIEWS,
        "active_view": view_key,
        "autom_view": automatizaciones.VIEW_BY_KEY[view_key],
        "panel_template": automatizaciones.panel_template(view_key),
    }
    builder = AUTOM_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))
    return context


def _productos_context(request) -> dict:
    """Data for the Productos panel: the tab row and the filtered queryset.

    ``?tab=`` selects the active tab. An unknown value falls back to the
    default rather than 404-ing, so a stale bookmark still opens.
    """
    tab_key = request.GET.get("tab", comercio.DEFAULT_TAB)
    if tab_key not in comercio.TAB_BY_KEY:
        tab_key = comercio.DEFAULT_TAB

    return {
        "tabs": comercio.TABS,
        "active_tab": tab_key,
        "columns": comercio.TABLE_COLUMNS,
        "products": comercio.get_products(tab_key),
    }


#: Mi comercio view key -> callable(request) -> dict. Views without an entry
#: need no data.
COMERCIO_PANEL_CONTEXT = {
    "productos": _productos_context,
}


def _comercio_context(request) -> dict:
    """Extra context for the Mi comercio screen.

    ``?view=`` selects the active row in the secondary nav. An unknown value
    falls back to the default rather than 404-ing, so a stale bookmark opens.
    """
    view_key = request.GET.get("view", comercio.DEFAULT_VIEW)
    if view_key not in comercio.VIEW_BY_KEY:
        view_key = comercio.DEFAULT_VIEW

    context = {
        "comercio_sections": comercio.SECTIONS,
        "comercio_single": comercio.STANDALONE,
        "active_view": view_key,
        "comercio_view": comercio.VIEW_BY_KEY[view_key],
        "panel_template": comercio.panel_template(view_key),
    }
    builder = COMERCIO_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))
    return context


def _mensajeria_stats_context(request) -> dict:
    """Data for the Mensajería panel: the four stat cards."""
    return {"stat_cards": estadisticas.CARDS}


def _etiquetas_stats_context(request) -> dict:
    """Data for the Etiquetas stats panel: every tag with how many
    conversations carry it, plus the tagged/untagged split for the tiles.

    Counts are annotated in one query, same stance as :func:`_etiquetas_context`
    (one through-row per (conversation, tag), so the count *is* "chats with
    this tag"). Archived tags keep their place in the ranking but sort below
    the active ones -- they still label old conversations, so their numbers
    are real history, just visually retired.
    """
    tags = list(
        Tag.objects.annotate(chats=Count("conversation_tags")).order_by(
            "is_archived", "-chats", "name"
        )
    )
    total = Conversation.objects.count()
    tagged = Conversation.objects.filter(tags__isnull=False).distinct().count()
    return {
        "tag_stats": tags,
        # The busiest tag's count -- what every row's bar is scaled against.
        "max_chats": max((tag.chats for tag in tags), default=0),
        "active_tag_count": sum(1 for tag in tags if not tag.is_archived),
        "archived_tag_count": sum(1 for tag in tags if tag.is_archived),
        "total_conversations": total,
        "tagged_conversations": tagged,
        "untagged_conversations": total - tagged,
    }


def _temas_stats_context(request) -> dict:
    """Data for the Temas de conversación panel.

    ``?period=`` picks the window; an unknown value falls back to the
    default rather than erroring, and the report itself lives in
    core.estadisticas_temas.
    """
    period = estadisticas_temas.parse_period(request.GET)
    return {
        "periods": estadisticas_temas.PERIODS,
        "period": period,
        "report": estadisticas_temas.report(period),
    }


def _ventas_stats_context(request) -> dict:
    """Data for the Ventas panel. Same period contract as
    :func:`_temas_stats_context`; the report lives in
    core.estadisticas_ventas.
    """
    period = estadisticas_periodos.parse_period(request.GET)
    return {
        "periods": estadisticas_periodos.PERIODS,
        "period": period,
        "report": estadisticas_ventas.report(period),
    }


def _embudos_stats_context(request) -> dict:
    """Data for the Embudos panel (the conversation funnel). Same period
    contract as the Temas and Ventas panels; the report lives in
    core.estadisticas_embudos.
    """
    period = estadisticas_periodos.parse_period(request.GET)
    return {
        "periods": estadisticas_periodos.PERIODS,
        "period": period,
        "report": estadisticas_embudos.report(period),
    }


def _atribuciones_stats_context(request) -> dict:
    """Data for the Atribuciones panel (channel attribution). Same period
    contract as its sibling panels; the report lives in
    core.estadisticas_atribuciones.
    """
    period = estadisticas_periodos.parse_period(request.GET)
    return {
        "periods": estadisticas_periodos.PERIODS,
        "period": period,
        "report": estadisticas_atribuciones.report(period),
    }


#: Estadísticas view key -> callable(request) -> dict. Views without an entry
#: need no data.
STATS_PANEL_CONTEXT = {
    "mensajeria": _mensajeria_stats_context,
    "ventas": _ventas_stats_context,
    "etiquetas": _etiquetas_stats_context,
    "embudos": _embudos_stats_context,
    "atribuciones": _atribuciones_stats_context,
    "temas-conversacion": _temas_stats_context,
}


def _estadisticas_context(request) -> dict:
    """Extra context for the Estadísticas screen.

    ``?view=`` selects the active row in the secondary nav. An unknown value
    falls back to the default rather than 404-ing, so a stale bookmark opens.
    """
    view_key = request.GET.get("view", estadisticas.DEFAULT_VIEW)
    if view_key not in estadisticas.VIEW_BY_KEY:
        view_key = estadisticas.DEFAULT_VIEW

    context = {
        "stats_nav": estadisticas.VIEWS,
        "active_view": view_key,
        "stats_view": estadisticas.VIEW_BY_KEY[view_key],
        "panel_template": estadisticas.panel_template(view_key),
    }
    builder = STATS_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))
    return context


def _plantillas_context(request) -> dict:
    """Data for the Plantillas de WhatsApp panel: the tab row and the
    filtered queryset.

    ``?tab=`` selects the active tab. An unknown value falls back to the
    default rather than 404-ing, so a stale bookmark still opens.
    """
    tab_key = request.GET.get("tab", mensajeria.DEFAULT_TAB)
    if tab_key not in mensajeria.TAB_BY_KEY:
        tab_key = mensajeria.DEFAULT_TAB

    return {
        "tabs": mensajeria.TABS,
        "active_tab": tab_key,
        "columns": mensajeria.TABLE_COLUMNS,
        "templates": mensajeria.get_templates(tab_key),
    }


def _respuestas_context(request) -> dict:
    """Data for the Respuestas rápidas panel: every quick reply, active ones
    first, with who wrote it."""
    return {
        "replies": QuickReply.objects.select_related("created_by").order_by(
            "-is_active", "title"
        ),
    }


#: Configuración de mensajería view key -> callable(request) -> dict. Views
#: without an entry need no data.
def _automations_context(request) -> dict:
    """Shared by the three automation screens: the one settings row, plus the
    agents auto-assignment would actually rotate through (so the page can say
    "nobody configured" instead of silently doing nothing)."""
    return {
        "msg_settings": MessagingSettings.load(),
        "assign_agents": agents.agent_users(),
        "widget_positions": MessagingSettings.WIDGET_POSITIONS,
    }


MENSAJERIA_PANEL_CONTEXT = {
    "plantillas-whatsapp": _plantillas_context,
    "respuestas-rapidas": _respuestas_context,
    "mensajes-bienvenida": _automations_context,
    "asignacion-automatica": _automations_context,
    "widget-whatsapp": _automations_context,
}


def _mensajeria_context(request) -> dict:
    """Extra context for the Configuración de mensajería screen.

    ``?view=`` selects the active row in the secondary nav. An unknown value
    falls back to the default rather than 404-ing, so a stale bookmark opens.
    """
    view_key = request.GET.get("view", mensajeria.DEFAULT_VIEW)
    if view_key not in mensajeria.VIEW_BY_KEY:
        view_key = mensajeria.DEFAULT_VIEW

    context = {
        "msg_nav": mensajeria.VIEWS,
        "active_view": view_key,
        "msg_view": mensajeria.VIEW_BY_KEY[view_key],
        "panel_template": mensajeria.panel_template(view_key),
    }
    builder = MENSAJERIA_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))
    return context


#: section key -> callable(request) -> dict of extra template context.
SECTION_CONTEXT = {
    "inbox": _inbox_context,
    "crm": _crm_context,
    # Campañas mounts the same account nav and panels as the CRM (core.crm).
    "campanas": _crm_context,
    "embudos": _embudos_context,
    "automatizaciones": _automatizaciones_context,
    "mi-comercio": _comercio_context,
    "estadisticas": _estadisticas_context,
    "mensajeria": _mensajeria_context,
}


# --- Views -----------------------------------------------------------------


def login_view(request):
    """The one gate in front of the whole app -- see core.middleware.

    Credentials are checked against the database (``core.agents``, which
    imports the environment's seed agents first), and a successful login
    starts a *real* ``django.contrib.auth`` session as that User. That is
    what makes the agent an identity rather than a boolean: "Tu inbox" can
    filter ``assigned_to=request.user``, outbound messages record who wrote
    them, and the Inbox's assignment dropdown has a sensible default.
    """
    error = None
    next_url = request.GET.get('next') or request.POST.get('next') or reverse('home')
    # ?next is attacker-reachable (it's a query param on a link anyone can send)
    # -- without this check a crafted next=https://evil.example would redirect
    # a successful login straight off the site, the classic post-login
    # open-redirect phishing setup.
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse('home')
    if request.method == 'POST':
        user = agents.authenticate(
            request.POST.get('username', ''), request.POST.get('password', '')
        )
        if user is not None:
            # core.agents.authenticate already verified the password (and
            # applied the app's own rules: active, not a /admin account), so
            # name the backend rather than run Django's authenticate() again.
            auth_login(
                request,
                user,
                backend='django.contrib.auth.backends.ModelBackend',
            )
            # After auth_login: it cycles the session key, and the gate flag
            # must survive into the new session.
            request.session[SESSION_KEY] = True
            return redirect(next_url)
        error = 'Usuario o contraseña incorrectos.'
    return HttpResponse(
        render_to_string(
            'login.html', {'error': error, 'next': next_url}, request=request
        )
    )


def logout_view(request):
    auth_logout(request)
    request.session.flush()
    return redirect('login')


def welcome(request):
    """Render the landing screen served at "/".

    This is the shell's resting state: the app is open but no section has been
    picked yet. The only thing that makes it special is ``active_key=None`` --
    :file:`partials/nav_item.html` compares every ``item.key`` against it, so a
    value no key can equal leaves the whole rail unselected.

    It deliberately does *not* go through :func:`section`: the root URL is not a
    nav destination, and keeping it separate is what stops "/" from resolving to
    a section and lighting up an icon.
    """
    context = {
        "primary_nav": PRIMARY_NAV,
        "secondary_nav": SECONDARY_NAV,
        "active_key": None,         # no icon is selected on the welcome screen
        "page_title": "Bienvenido",
        "shortcuts": [NAV_BY_KEY[key] for key in WELCOME_SHORTCUTS],
        "section_template": "sections/welcome.html",
    }

    if _is_htmx(request):
        return HttpResponse(
            render_to_string(context["section_template"], context, request=request)
        )

    return HttpResponse(render_to_string("base.html", context, request=request))


def section(request, key: str = DEFAULT_SECTION):
    """Render one nav section, as a fragment for HTMX or a full page otherwise."""
    item = NAV_BY_KEY.get(key)
    if item is None:
        raise Http404(f"Unknown section: {key!r}")

    context = {
        "primary_nav": PRIMARY_NAV,
        "secondary_nav": SECONDARY_NAV,
        "active_key": key,          # drives the active pill in the sidebar
        "item": item,               # the section's own label / icon
        "page_title": item.label,
        "section_template": _section_template(key),
    }

    # Let the section contribute its own data, if it registered a builder.
    builder = SECTION_CONTEXT.get(key)
    if builder is not None:
        context.update(builder(request))

    if _is_htmx(request):
        # Fragment only -- hx-swap="innerHTML" drops this inside <main id="content">.
        return HttpResponse(
            render_to_string(context["section_template"], context, request=request)
        )

    return HttpResponse(render_to_string("base.html", context, request=request))


def inbox_list(request, filter_key: str):
    """Return just the conversation list (Inbox column 3) for one filter.

    Targeted by the nav panel's HTMX requests (picking a filter) and by the
    list's own 5-second poll, which is how new inbound conversations appear
    without a refresh. ``?active=`` carries the open conversation's id so a
    poll re-render keeps its row highlighted.
    """
    if filter_key not in inbox.FILTER_BY_KEY:
        raise Http404(f"Unknown filter: {filter_key!r}")

    # Let the fake provider deliver due receipts even when no chat is open,
    # so tick marks keep moving while only the list is polling. No-op on
    # real (push-based) providers.
    messaging_services.pump_provider_events()

    try:
        active_id = int(request.GET.get("active", ""))
    except ValueError:
        active_id = None

    return HttpResponse(
        render_to_string(
            "partials/inbox/conversation_list.html",
            {
                "active_filter": filter_key,
                "conversations": inbox.get_conversations(
                    filter_key, request.user, _selected_tag_ids(request.GET)
                ),
                "active_conversation_id": active_id,
            },
            request=request,
        )
    )


def inbox_chat(request, conversation_id: int):
    """Open one conversation: chat panel content plus, out-of-band, the
    details panel -- one click updates both columns in a single response.

    Targeted by the conversation rows' HTMX requests into #chat-panel; the
    ``hx-swap-oob`` block inside the template lands in #details-panel.
    """
    conversation = get_object_or_404(
        Conversation.objects.select_related("contact", "assigned_to"),
        pk=conversation_id,
    )
    _mark_read(conversation)

    return HttpResponse(
        render_to_string(
            "partials/inbox/chat_swap.html", _thread_context(conversation), request=request
        )
    )


def inbox_thread(request, conversation_id: int):
    """Return just the message list of one conversation.

    Targeted by the open thread's 5-second poll, which is how replies (and
    the fake provider's delivery receipts) appear without a refresh. Polling
    swaps only #chat-messages, so a half-typed draft in the composer below
    is never clobbered -- except in the one moment the composer itself has
    to change, when the 24h window has flipped (see below).
    """
    conversation = get_object_or_404(Conversation, pk=conversation_id)

    # Pull-based providers (the fake one) deliver their pending status events
    # on this tick; the thread then renders with fresh tick marks.
    messaging_services.pump_provider_events()

    # Still looking at the thread -- an inbound that arrived since the last
    # poll is read the moment it renders.
    _mark_read(conversation)

    context = _thread_context(conversation)
    html = render_to_string("partials/inbox/chat_messages.html", context, request=request)

    # The page reports which composer it is showing (chat_composer.html's
    # #chat-window-state). If the 24h window has flipped since -- the
    # customer just replied, or the silence ran past 24 hours -- the other
    # composer rides along out-of-band, so the box unlocks (or locks) with
    # nobody reopening the chat. Only then: an unchanged state never touches
    # the composer, and a poll that reports nothing (an old tab, another
    # caller) gets exactly the message list, as before.
    shown = request.GET.get("window_open")
    if shown in ("0", "1") and (shown == "1") != context["window_open"]:
        html += render_to_string(
            "partials/inbox/chat_composer.html", {**context, "oob": True}, request=request
        )
    return HttpResponse(html)


#: The thread's notice for a send WhatsApp took but never confirmed
#: (``messaging.services.SendUnconfirmed``). Deliberately not "inténtalo de
#: nuevo": the message has most likely arrived, and a retry would send the
#: customer a duplicate. The row keeps its clock until the receipt lands.
_SEND_UNCONFIRMED_NOTICE = (
    "WhatsApp no confirmó el envío a tiempo. Quedó pendiente (reloj): no lo "
    "reenvíes, se actualizará solo cuando llegue la confirmación."
)


def inbox_send(request, conversation_id: int):
    """Send the composer's message, answering with the refreshed thread.

    The 24h-window rule is enforced in ``messaging.services.send_message``;
    the composer isn't even rendered when the window is closed, so tripping
    it here means the window expired while the thread was open -- answered
    with an inline notice rather than a broken send.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    conversation = get_object_or_404(
        Conversation.objects.select_related("contact"), pk=conversation_id
    )

    body = (request.POST.get("body") or "").strip()
    image_url = ""
    # A quick reply arrives by id, not by text: the server resolves what it
    # says and whether it carries an image, so the picker can't be made to
    # send anything the reply doesn't hold.
    reply_id = request.POST.get("quick_reply")
    if reply_id:
        reply = QuickReply.objects.filter(pk=reply_id, is_active=True).first()
        if reply is not None:
            body = reply.body
            image_url = respuestas.image_url(reply, request)

    send_error = None
    send_pending = False
    if body or image_url:
        try:
            messaging_services.send_message(
                conversation, body, request.user, image_url=image_url
            )
        except messaging_services.SendWindowClosed:
            send_error = (
                "La ventana de 24 horas se cerró. Envía una plantilla "
                "aprobada para reabrir la conversación."
            )
        except messaging_services.SendUnconfirmed:
            # Before SendFailed: it is a subclass, and the opposite advice.
            send_error, send_pending = _SEND_UNCONFIRMED_NOTICE, True
        except messaging_services.SendFailed:
            send_error = "No se pudo enviar el mensaje. Inténtalo de nuevo."

    context = _thread_context(conversation)
    context["send_error"] = send_error
    context["send_pending"] = send_pending
    return HttpResponse(
        render_to_string("partials/inbox/chat_messages.html", context, request=request)
    )


def inbox_quick_replies(request, conversation_id: int):
    """The Respuestas rápidas popover: the account's quick replies, each one
    click away from landing in *this* conversation.

    Fetched lazily the first time the picker opens (see chat_thread.html).
    Lists every active :class:`core.models.QuickReply` -- the team's own
    canned answers, managed in Configuración de mensajería > Respuestas
    rápidas. Each entry posts its id to :func:`inbox_send`, which resolves
    the text and the image server-side. (The picker used to list WhatsApp
    plantillas; those now belong to the Enviar plantilla flow, the only
    thing allowed outside the 24h window.)
    """
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    replies = QuickReply.objects.filter(is_active=True).order_by("title")
    return HttpResponse(
        render_to_string(
            "partials/inbox/quick_replies.html",
            {"replies": replies, "conversation": conversation},
            request=request,
        )
    )


# --- Enviar plantilla ---------------------------------------------------------


def _sendable_templates():
    """The plantillas the Enviar plantilla dialog offers: active and not
    rechazada. Pendientes are in -- the MVP has no approval pipeline of its
    own, so shutting them out would hide every plantilla ever created here;
    the provider is the authority on whether an unapproved one goes."""
    return (
        MessageTemplate.objects.filter(is_active=True)
        .exclude(status="rechazada")
        .order_by("name")
    )


def _template_variables(template, values=None) -> list[dict]:
    """One entry per {{n}} in the body with the value its input should show.

    A fresh dialog (``values`` is None) prefills each with the editor's
    sample. A rejected submit passes what was typed, blanks included -- and
    a blank must come back *blank*, not quietly refilled with the sample, or
    "completa todas las variables" would point at a form with nothing empty.
    """
    samples = template.body_sample_values or []
    return [
        {
            "number": number,
            "value": (
                values.get(str(number), "")
                if values is not None
                else (samples[number - 1] if number - 1 < len(samples) else "")
            ),
        }
        for number in plantillas.body_variables(template.body)
    ]


def _template_send_body_context(conversation, selected=None, values=None, error=None) -> dict:
    """The Enviar plantilla dialog's contents for one conversation.

    Every entry carries its own price. A template send is billed per message
    by the plantilla's category and the recipient's market, and
    ``services.send_template`` records that amount either way -- showing it
    here is what lets the agent see the cost *before* pressing Enviar rather
    than afterwards on the invoice.

    The quote is an estimate: Meta charges on delivery, at the category it
    assigned the plantilla, and ``services._apply_pricing`` corrects the row
    when the receipt arrives. ``budget`` is the month's running total, plus
    the ceiling when ``MESSAGING_MONTHLY_BUDGET`` sets one.
    """
    templates = list(_sendable_templates())
    # One window check for the whole dialog: it is the same conversation for
    # every entry, and it changes the price (a utility plantilla inside an
    # open window is billed as a service message).
    window_open = conversation.is_within_24h_window
    # Likewise counted once. Every entry is priced against the same month, so
    # asking per plantilla would be one query per row of the picker.
    service_used = pricing.service_used_this_month() if window_open else 0
    return {
        "active_conversation": conversation,
        "entries": [
            {
                "template": template,
                "variables": _template_variables(
                    template, values if selected == template else None
                ),
                "quote": pricing.quote(
                    template,
                    conversation.contact,
                    window_open=window_open,
                    service_used=service_used,
                ),
            }
            for template in templates
        ],
        "selected_id": selected.pk if selected else (templates[0].pk if templates else None),
        "send_form_error": error,
        "budget": pricing.budget_state(),
    }


def inbox_template_send(request, conversation_id: int):
    """The Enviar plantilla dialog for one conversation: GET renders it, POST
    sends the chosen plantilla with its {{n}} filled in.

    This is the way out of a closed 24h window -- the only send that works
    there -- but it is offered inside the window too, since a plantilla sent
    as a *template* (buttons, header, Meta's own rendering) is not the same
    thing as its wording pasted into the composer.

    Answers: GET -> the <dialog> (dropped into the composer's slot and opened
    by shell.js); a valid POST -> the refreshed thread, like inbox_send, with
    the dialog closing on success; a POST missing a variable value -> 422
    with just the dialog's body re-rendered into place, error included, so
    the agent fixes the blank instead of starting over.
    """
    conversation = get_object_or_404(
        Conversation.objects.select_related("contact"), pk=conversation_id
    )

    if request.method == "GET":
        context = _template_send_body_context(conversation)
        return HttpResponse(
            render_to_string(
                "partials/inbox/template_send_dialog.html", context, request=request
            )
        )
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])

    template = _sendable_templates().filter(pk=request.POST.get("template") or 0).first()
    if template is None:
        return _template_send_rejected(
            request, conversation, None, {}, "Elige una plantilla de la lista."
        )

    values = {
        str(number): (request.POST.get(f"var_{template.pk}_{number}") or "").strip()
        for number in plantillas.body_variables(template.body)
    }
    if any(not value for value in values.values()):
        return _template_send_rejected(
            request, conversation, template, values,
            "Completa todas las variables antes de enviar.",
        )

    send_error = None
    send_pending = False
    try:
        messaging_services.send_template(conversation, template, values, request.user)
    except (
        messaging_services.TemplateNotSendable,
        # A template send is billed, so it can also be refused for costing
        # too much this month (MESSAGING_MONTHLY_BUDGET). Same surface as any
        # other refusal: the dialog comes back with the reason, not a 500.
        messaging_services.BudgetExceeded,
    ) as exc:
        return _template_send_rejected(request, conversation, template, values, str(exc))
    except messaging_services.SendUnconfirmed:
        send_error, send_pending = _SEND_UNCONFIRMED_NOTICE, True
    except messaging_services.SendFailed:
        # Same surface as a failed free-form send: the thread shows it.
        send_error = "No se pudo enviar la plantilla. Inténtalo de nuevo."

    context = _thread_context(conversation)
    context["send_error"] = send_error
    context["send_pending"] = send_pending
    return HttpResponse(
        render_to_string("partials/inbox/chat_messages.html", context, request=request)
    )


def _template_send_rejected(request, conversation, template, values, error) -> HttpResponse:
    """422 + HX-Retarget into the open dialog -- the same shape the
    Respuestas rápidas dialog uses, so the form (and the agent's typing)
    survives the swap."""
    context = _template_send_body_context(conversation, template, values, error)
    response = HttpResponse(
        render_to_string("partials/inbox/template_send_body.html", context, request=request),
        status=422,
    )
    response["HX-Retarget"] = "#tpl-send-body"
    response["HX-Reswap"] = "innerHTML"
    return response


# --- Nuevo chat / plantillas ------------------------------------------------
#
# WhatsApp's rule: a business may only *start* a conversation (or reopen one
# the customer left more than 24h ago) with a pre-approved plantilla. Two
# doors, one picker:
#
# * "Nuevo Chat" in the Inbox nav -- pick a client, pick a plantilla, send.
#   The thread appears in the list and opens on the right.
# * A closed composer -- the same plantilla picker sits where the composer
#   would be, posting into that conversation.


def _template_options():
    """The plantillas the Nuevo chat picker offers, body rendered with its
    samples for the preview line. The same queryset send_template accepts,
    so nothing on offer can be refused as not sendable."""
    return [
        {"template": template, "body": plantillas.render_body(template)}
        for template in _sendable_templates()
    ]


def _new_chat_response(request, client=None, error=None, template=None):
    """The Nuevo chat modal body: a searchable client list, the plantilla
    picker and -- once a plantilla is chosen -- an input per {{n}}.

    ``template`` is the plantilla a rejected submit had selected, so the
    re-render comes back on the same one with what was typed still there.
    """
    values = None
    if template is not None and request.method == "POST":
        values = {
            str(entry["number"]): (
                request.POST.get(f"var_{template.pk}_{entry['number']}") or ""
            ).strip()
            for entry in _template_variables(template)
        }
    return HttpResponse(
        render_to_string(
            "partials/inbox/new_chat.html",
            {
                "clients": Client.objects.order_by("first_name", "last_name"),
                "selected_client": client,
                "template_options": _template_options(),
                "selected_template": template,
                "new_chat_url": reverse("inbox_new_chat"),
                "template_variables": (
                    _template_variables(template, values) if template else []
                ),
                "new_chat_error": error,
            },
            request=request,
        )
    )


def inbox_new_chat(request):
    """Start a conversation from our side.

    GET renders the modal body (``?cliente=`` preselects one). POST takes
    ``cliente`` + ``plantilla``, sends the plantilla through
    messaging.services.send_template into the client's open WhatsApp thread
    (or a new one) and answers with the opened chat -- the same fragment a
    row click produces -- plus the refreshed list out-of-band, and the
    dismiss marker that closes the modal.
    """
    if request.method == "GET":
        client = Client.objects.filter(pk=request.GET.get("cliente") or 0).first()
        # The picker checks the first plantilla when none is named, so the
        # body must render THAT one's variable inputs too -- otherwise a
        # first open shows a checked radio and no inputs, and the first
        # submit bounces on "completa las variables" for fields that were
        # never on screen.
        sendable = _sendable_templates()
        chosen = (
            sendable.filter(pk=request.GET.get("plantilla") or 0).first()
            or sendable.first()
        )
        return _new_chat_response(request, client, None, chosen)
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])

    client = Client.objects.filter(pk=request.POST.get("cliente") or 0).first()
    template = (
        MessageTemplate.objects.filter(
            pk=request.POST.get("plantilla") or 0, is_active=True
        )
        .exclude(status="rechazada")
        .first()
    )
    if client is None:
        return _new_chat_response(request, None, "Elige a quién escribirle.")
    if template is None:
        return _new_chat_response(request, client, "Elige una plantilla para abrir la conversación.")

    # The agent fills the plantilla's {{n}} for THIS client; the editor's
    # samples are examples for Meta's reviewer, not a greeting for a stranger.
    values = {
        str(entry["number"]): (
            request.POST.get(f"var_{template.pk}_{entry['number']}") or ""
        ).strip()
        for entry in _template_variables(template)
    }
    if any(not value for value in values.values()):
        return _new_chat_response(
            request, client, "Completa todas las variables de la plantilla.", template
        )

    conversation = messaging_services.start_conversation(client, "whatsapp")
    try:
        messaging_services.send_template(conversation, template, values, request.user)
    except (
        messaging_services.TemplateNotSendable,
        # As in the composer's dialog: over the month's ceiling is a refusal
        # the agent should read, not a crash.
        messaging_services.BudgetExceeded,
    ) as exc:
        return _new_chat_response(request, client, str(exc), template)
    except messaging_services.SendFailed:
        # The provider's own text is for the log, not for the agent.
        return _new_chat_response(
            request, client, "No se pudo enviar la plantilla. Inténtalo de nuevo.", template
        )

    _mark_read(conversation)
    context = _thread_context(conversation)
    context.update(
        {
            "active_filter": inbox.DEFAULT_FILTER,
            "conversations": inbox.get_conversations(inbox.DEFAULT_FILTER, request.user),
        }
    )
    return HttpResponse(
        render_to_string("partials/inbox/new_chat_opened.html", context, request=request)
    )


# --- Assignment -------------------------------------------------------------


def inbox_assign(request, conversation_id: int):
    """Set (or clear) the agent answering one conversation.

    Posted by the dropdown in the chat header on every change. The answer is
    the re-rendered control plus, out-of-band, the details panel's "Asignada a"
    line -- the conversation list's own poll picks the change up on its next
    tick, so nothing here has to know whether that row is even on screen.

    An id that isn't a configured agent is rejected rather than saved: the
    dropdown is a fixed list, so anything else is a hand-crafted POST.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    conversation = get_object_or_404(
        Conversation.objects.select_related("contact", "assigned_to"),
        pk=conversation_id,
    )

    # What the dropdown was rendered with, and so what a POST may name.
    choices = agents.assignment_options(conversation)
    raw = (request.POST.get("agent") or "").strip()
    if raw:
        # Only ids from the rendered options are accepted -- an unknown one
        # leaves the assignment untouched instead of pointing it at some
        # arbitrary User row.
        assignee = next((user for user in choices if str(user.pk) == raw), None)
        if assignee is None:
            return HttpResponse(status=400)
    else:
        assignee = None

    if conversation.assigned_to_id != getattr(assignee, "pk", None):
        conversation.assigned_to = assignee
        conversation.save(update_fields=["assigned_to"])

    return HttpResponse(
        render_to_string(
            "partials/inbox/assign_swap.html",
            {
                "active_conversation": conversation,
                # Recomputed *after* the save, not reusing ``choices``: the
                # options include the current assignee, so moving a chat off
                # someone who is no longer in APP_AGENTS should drop them from
                # the list rather than leave them there until the next reload.
                "assign_options": agents.assignment_options(conversation),
            },
            request=request,
        )
    )


# --- Tags -------------------------------------------------------------------


def _picker_context(request, conversation, error=None) -> dict:
    """Context for the tag-picker panel of one conversation.

    ``?q=`` narrows the tag list; when it matches nothing exactly, the panel
    offers to create «q» inline. Archived tags never appear -- they exist
    only as history on already-tagged chats.
    """
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    tags = Tag.objects.filter(is_archived=False)
    if q:
        tags = tags.filter(name__icontains=q)
    return {
        "conversation": conversation,
        "picker_tags": tags,
        "applied_ids": set(conversation.tags.values_list("pk", flat=True)),
        "q": q,
        "offer_create": bool(q) and not Tag.objects.filter(name__iexact=q).exists(),
        "tag_error": error,
    }


def conversation_tags(request, conversation_id: int):
    """The tag picker for one conversation: GET renders it, POST mutates.

    POST either toggles an existing tag (``tag_id`` + ``action`` add/remove)
    or creates-and-applies a brand-new one (``new_name``, the picker's inline
    «Crear ...» path). The response is the refreshed picker plus out-of-band
    updates of that conversation's pill rows (list row and chat header), so
    all three surfaces agree immediately.
    """
    conversation = get_object_or_404(Conversation, pk=conversation_id)

    if request.method == "POST":
        error = None
        new_name = (request.POST.get("new_name") or "").strip()
        try:
            if new_name:
                # Color is auto-assigned here (rotating palette); the user
                # can restyle it later in CRM > Etiquetas.
                tag = messaging_services.create_tag(new_name, user=request.user)
                messaging_services.apply_tag([conversation], tag, request.user)
            else:
                tag = get_object_or_404(Tag, pk=request.POST.get("tag_id"))
                if request.POST.get("action") == "remove":
                    messaging_services.remove_tag([conversation], tag)
                else:
                    messaging_services.apply_tag([conversation], tag, request.user)
        except (messaging_services.TagNameTaken, ValueError) as exc:
            error = str(exc)

        context = _picker_context(request, conversation, error)
        context["oob_pills"] = True
        return HttpResponse(
            render_to_string("partials/tags/picker_panel.html", context, request=request)
        )

    return HttpResponse(
        render_to_string(
            "partials/tags/picker_panel.html",
            _picker_context(request, conversation),
            request=request,
        )
    )


def inbox_tags_bulk(request):
    """Apply/remove one tag on every checked conversation at once.

    Posted from the list's "Acciones" dropdown; the enclosing form carries
    the checked ids (``selected``), the current nav filter (hidden input kept
    fresh by every list render) and the tag-filter state, so the response is
    the re-rendered list under exactly the filters the user was looking at.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    tag = get_object_or_404(Tag, pk=request.POST.get("tag_id"))
    ids = [i for i in request.POST.getlist("selected") if i.isdigit()]
    conversations = Conversation.objects.filter(pk__in=ids)

    if request.POST.get("action") == "remove":
        messaging_services.remove_tag(conversations, tag)
    else:
        messaging_services.apply_tag(conversations, tag, request.user)

    filter_key = request.POST.get("filter", inbox.DEFAULT_FILTER)
    if filter_key not in inbox.FILTER_BY_KEY:
        filter_key = inbox.DEFAULT_FILTER
    try:
        active_id = int(request.POST.get("active", ""))
    except ValueError:
        active_id = None

    return HttpResponse(
        render_to_string(
            "partials/inbox/conversation_list.html",
            {
                "active_filter": filter_key,
                "conversations": inbox.get_conversations(
                    filter_key, request.user, _selected_tag_ids(request.POST)
                ),
                "active_conversation_id": active_id,
            },
            request=request,
        )
    )


def _tag_table_response(request, error=None):
    """The re-rendered #tag-table region every tag mutation answers with."""
    context = _etiquetas_context(request)
    context["tag_error"] = error
    return HttpResponse(
        render_to_string("partials/crm/tag_table.html", context, request=request)
    )


def tag_create(request):
    """Create a tag from the Etiquetas page's modal."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    error = None
    try:
        messaging_services.create_tag(
            request.POST.get("name", ""), request.POST.get("color") or None, request.user
        )
    except (messaging_services.TagNameTaken, ValueError) as exc:
        error = str(exc)
    return _tag_table_response(request, error)


def tag_update(request, tag_id: int):
    """Rename/recolor a tag -- every pill in the app updates with it."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tag = get_object_or_404(Tag, pk=tag_id)
    error = None
    try:
        messaging_services.update_tag(
            tag, request.POST.get("name", ""), request.POST.get("color", tag.color)
        )
    except (messaging_services.TagNameTaken, ValueError) as exc:
        error = str(exc)
    return _tag_table_response(request, error)


def tag_archive(request, tag_id: int):
    """Archive (``archived=1``) or restore (``archived=0``) a tag.

    There is deliberately no hard delete: archiving hides the tag from
    pickers while every already-tagged conversation keeps it.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tag = get_object_or_404(Tag, pk=tag_id)
    messaging_services.set_tag_archived(tag, request.POST.get("archived") == "1")
    return _tag_table_response(request)


def crm_panel(request, view_key: str):
    """Return just the CRM's column 3 for one secondary-nav view.

    Targeted by the nav panel's HTMX requests so picking a page re-renders only
    the panel, leaving the nav (and its collapsed/expanded state) untouched.
    """
    if view_key not in crm.VIEW_BY_KEY:
        raise Http404(f"Unknown CRM view: {view_key!r}")

    context = {
        "active_view": view_key,
        "crm_view": crm.VIEW_BY_KEY[view_key],
    }
    builder = PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(crm.panel_template(view_key), context, request=request)
    )


def clientes_table(request):
    """Return just the client table region (rows + pager) for one ``?page=``.

    Targeted by the pager's HTMX requests so paging re-renders only
    #client-table -- the title and toolbar above it stay put. The pager used
    to fetch the *full* Clientes panel into that region, nesting a second
    toolbar and a duplicate #client-table inside the first on every click.
    """
    return HttpResponse(
        render_to_string(
            "partials/crm/client_table.html", _clientes_context(request), request=request
        )
    )


# --- Clientes CRUD ----------------------------------------------------------
#
# The four screens all render into one shared modal (#client-modal-body in
# panels/clientes.html) rather than a dialog per row: at 25 rows a page, three
# pre-rendered dialogs each would triple the panel's HTML for markup nobody
# opens. Fetching the one that was asked for keeps the table light.
#
# A successful save answers with partials/crm/client_saved.html, which closes
# the modal and swaps the refreshed table in out-of-band. A rejected one
# answers with the form again -- errors inline, everything typed still there.


def _client_form_response(request, state, errors, client=None):
    """The create/edit dialog body, rendered for the modal."""
    return HttpResponse(
        render_to_string(
            "partials/crm/client_form.html",
            {
                "form": state,
                "errors": errors,
                "client": client,
                "countries": clientes.COUNTRIES,
                "channels": Client.CHANNEL_CHOICES,
                "q": _table_param(request, "q"),
                "page": _table_param(request, "page"),
            },
            request=request,
        )
    )


def _client_saved_response(request, message: str):
    """What every successful mutation answers with: close the modal, refresh
    the table under it."""
    context = _clientes_context(request)
    context["client_notice"] = message
    return HttpResponse(
        render_to_string("partials/crm/client_saved.html", context, request=request)
    )


def cliente_form(request, client_id: int | None = None):
    """The Crear/Editar cliente dialog: GET renders it, POST saves it.

    One view for both because the only difference is whether there is a row to
    update -- exactly the shape core.plantillas.plantilla_editor has, and the
    same error contract: invalid input re-renders the form with per-field
    messages instead of throwing away what was typed.
    """
    client = get_object_or_404(Client, pk=client_id) if client_id else None

    if request.method == "POST":
        state = clientes.form_state(request.POST)
        errors = clientes.validate(state, client)
        if not errors:
            saved = clientes.apply(state, client)
            return _client_saved_response(
                request,
                f"Cliente actualizado: {saved.full_name}."
                if client
                else f"Cliente creado: {saved.full_name}.",
            )
        return _client_form_response(request, state, errors, client)

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _client_form_response(request, clientes.form_state(client=client), {}, client)


def cliente_detail(request, client_id: int):
    """The read-only "Ver" card behind the eye button.

    Shows what the row can't fit: when the client came in, which channels they
    have threads on and which lists they belong to.
    """
    client = get_object_or_404(
        Client.objects.prefetch_related("conversations", "client_lists"), pk=client_id
    )
    return HttpResponse(
        render_to_string(
            "partials/crm/client_detail.html",
            {
                "client": client,
                "conversations": client.conversations.all(),
                "q": _table_param(request, "q"),
                "page": _table_param(request, "page"),
            },
            request=request,
        )
    )


def cliente_delete(request, client_id: int):
    """GET asks; POST deletes.

    The confirmation is not decoration: ``Conversation.contact`` cascades, so
    deleting a client takes their whole message history with them. The GET
    fragment says how many threads that is before the agent commits.
    """
    client = get_object_or_404(Client, pk=client_id)

    if request.method == "POST":
        name = client.full_name
        client.delete()
        return _client_saved_response(request, f"Cliente eliminado: {name}.")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return HttpResponse(
        render_to_string(
            "partials/crm/client_delete.html",
            {
                "client": client,
                "conversation_count": client.conversations.count(),
                "q": _table_param(request, "q"),
                "page": _table_param(request, "page"),
            },
            request=request,
        )
    )


def clientes_export(request):
    """Download the client base as an Excel file (CRM > Exportaciones).

    Every client, one row each, in the CLIENT_EXPORT_COLUMNS order. Built
    with core.xlsx rather than a CSV: Excel opens a CSV with the wrong
    encoding and turns "+573167687288" into 5.73E+11, which is exactly the
    column people export this for. Conversation and tag columns come from
    two annotations/prefetches, not one query per row.
    """
    clients = (
        Client.objects.annotate(conversation_count=Count("conversations", distinct=True))
        .prefetch_related("conversations__tags")
        .order_by("first_name", "last_name")
    )
    country_names = {country.code: country.name for country in clientes.COUNTRIES}
    rows = []
    for client in clients:
        tags = sorted(
            {tag.name for conversation in client.conversations.all() for tag in conversation.tags.all()}
        )
        rows.append([
            client.first_name,
            client.last_name,
            client.phone,
            country_names.get(client.country, client.country),
            client.email,
            client.get_channel_display() if client.channel else "",
            timezone.localtime(client.created_at).strftime("%d/%m/%Y"),
            client.conversation_count,
            ", ".join(tags),
        ])

    stamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d")
    response = HttpResponse(
        xlsx.build(CLIENT_EXPORT_COLUMNS, rows, sheet_name="Clientes"),
        content_type=xlsx.CONTENT_TYPE,
    )
    response["Content-Disposition"] = f'attachment; filename="clientes-{stamp}.xlsx"'
    return response


# --- Usuarios (CRM > Equipo) ------------------------------------------------
#
# Same shared-modal shape as the Clientes CRUD. Every mutation is gated on
# core.agents.is_master: the response for anyone else is a fragment saying
# so, with a 403, so a stale button can't do anything.


def _forbidden_fragment(request):
    return HttpResponse(
        render_to_string("partials/crm/usuarios/forbidden.html", {}, request=request),
        status=403,
    )


def _user_table_fragment(request, notice=None):
    context = _usuarios_context(request)
    context["user_notice"] = notice
    return render_to_string("partials/crm/usuarios/table.html", context, request=request)


def _user_form_response(request, state, errors, user=None):
    return HttpResponse(
        render_to_string(
            "partials/crm/usuarios/form.html",
            {"form": state, "errors": errors, "edit_user": user},
            request=request,
        )
    )


def _user_saved_response(request, notice):
    context = _usuarios_context(request)
    context["user_notice"] = notice
    return HttpResponse(
        render_to_string("partials/crm/usuarios/saved.html", context, request=request)
    )


def _user_form_state(post=None, user=None) -> dict:
    if post is not None:
        return {
            "username": (post.get("username") or "").strip(),
            "display_name": (post.get("display_name") or "").strip(),
            "password": post.get("password") or "",
            "password2": post.get("password2") or "",
            "master": post.get("master") == "1",
        }
    if user is not None:
        return {
            "username": user.username,
            "display_name": user.get_full_name() or user.username,
            "password": "",
            "password2": "",
            "master": agents.is_master(user),
        }
    return {"username": "", "display_name": "", "password": "", "password2": "", "master": False}


def _validate_user_form(state: dict, editing) -> dict:
    errors = {}
    if not editing:
        username = state["username"]
        if not username:
            errors["username"] = "Escribe el usuario con el que iniciará sesión."
        elif len(username) > 150 or any(c in username for c in " :,"):
            errors["username"] = "Sin espacios, dos puntos ni comas; máximo 150 caracteres."
    password_required = not editing
    if password_required and not state["password"]:
        errors["password"] = "Ponle una contraseña."
    if state["password"]:
        if len(state["password"]) < 8:
            errors["password"] = "Mínimo 8 caracteres."
        elif state["password"] != state["password2"]:
            errors["password2"] = "Las contraseñas no coinciden."
    return errors


def usuario_form(request, user_id: int | None = None):
    """Crear/Editar usuario (masters only). Creating needs a password;
    editing may leave it blank to keep the current one."""
    if not agents.is_master(request.user):
        return _forbidden_fragment(request)
    User = get_user_model()
    user = get_object_or_404(User, pk=user_id) if user_id else None
    if user is not None and (user.is_staff or user.is_superuser):
        # A /admin account is not this page's to hand out -- see
        # core.agents._is_app_user.
        return _user_saved_response(
            request, f"{user.username} es una cuenta de Django admin; no se gestiona aquí."
        )
    if request.method == "POST":
        state = _user_form_state(request.POST)
        errors = _validate_user_form(state, editing=user is not None)
        if not errors:
            try:
                if user is None:
                    saved = agents.create_user(
                        state["username"], state["password"], state["display_name"], state["master"]
                    )
                    notice = f"Usuario creado: {saved.get_full_name() or saved.username}."
                else:
                    # A master can't demote themselves -- that would leave
                    # a team where nobody can manage anyone.
                    master = state["master"] or user.pk == request.user.pk
                    saved = agents.update_user(user, state["display_name"], master, state["password"])
                    if state["password"] and saved.pk == request.user.pk:
                        # Changing your own password rotates the session auth
                        # hash; without this the very next request logs you out.
                        update_session_auth_hash(request, saved)
                    notice = f"Usuario actualizado: {saved.get_full_name() or saved.username}."
            except agents.LastMaster as exc:
                # A message about the master role belongs beside the master
                # checkbox, not under the username field it says nothing about.
                errors["master"] = str(exc)
            except (agents.UsernameTaken, ValueError) as exc:
                errors["username"] = str(exc)
            else:
                return _user_saved_response(request, notice)
        return _user_form_response(request, state, errors, user)

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _user_form_response(request, _user_form_state(user=user), {}, user)


def usuario_active(request, user_id: int):
    """Deactivate (``active=0``) or restore (``active=1``) an app-created
    user. No hard delete: their history keeps pointing at them."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not agents.is_master(request.user):
        return _forbidden_fragment(request)
    user = get_object_or_404(get_user_model(), pk=user_id)
    active = request.POST.get("active") == "1"
    if user.pk == request.user.pk and not active:
        return HttpResponse(
            _user_table_fragment(request, "No puedes desactivar tu propio usuario.")
        )
    try:
        agents.set_user_active(user, active)
    except (agents.LastMaster, ValueError) as exc:
        return HttpResponse(_user_table_fragment(request, str(exc)))
    label = "restaurado" if active else "desactivado"
    return HttpResponse(
        _user_table_fragment(request, f"Usuario {label}: {user.get_full_name() or user.username}.")
    )


def lista_create(request):
    """Placeholder destination for the "+ Crear lista" button.

    Wired so the button goes somewhere real; the creation flow itself is not
    built yet.
    """
    return HttpResponse(
        render_to_string("partials/crm/panels/_nueva_lista.html", {}, request=request)
    )


def embudos_panel(request, view_key: str):
    """Return just the Embudos column 3 for one secondary-nav view.

    Targeted by the nav panel's HTMX requests so picking a page re-renders only
    the panel, leaving the nav untouched.
    """
    if view_key not in embudos.VIEW_BY_KEY:
        raise Http404(f"Unknown Embudos view: {view_key!r}")

    context = {
        "active_view": view_key,
        "embudos_view": embudos.VIEW_BY_KEY[view_key],
    }
    builder = EMBUDOS_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(embudos.panel_template(view_key), context, request=request)
    )


def embudo_create(request):
    """Placeholder destination for the "Crear nuevo embudo" button.

    Wired so the button goes somewhere real; the creation flow itself is not
    built yet.
    """
    return HttpResponse(
        render_to_string("partials/embudos/panels/_nuevo.html", {}, request=request)
    )


def automatizaciones_panel(request, view_key: str):
    """Return just the Automatizaciones column 3 for one secondary-nav view.

    Targeted by the nav panel's HTMX requests so picking a page re-renders only
    the panel, leaving the nav (and its expanded/collapsed state) untouched.
    """
    if view_key not in automatizaciones.VIEW_BY_KEY:
        raise Http404(f"Unknown Automatizaciones view: {view_key!r}")

    context = {
        "active_view": view_key,
        "autom_view": automatizaciones.VIEW_BY_KEY[view_key],
    }
    builder = AUTOM_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(
            automatizaciones.panel_template(view_key), context, request=request
        )
    )


def flujo_create(request):
    """Placeholder destination for the "+ Añadir flujo" button.

    Wired so the button goes somewhere real; the creation flow itself is not
    built yet.
    """
    return HttpResponse(
        render_to_string(
            "partials/automatizaciones/panels/_nuevo_flujo.html", {}, request=request
        )
    )


def comercio_panel(request, view_key: str):
    """Return just the Mi comercio column 3 for one secondary-nav view.

    Targeted by the nav panel's HTMX requests so picking a page re-renders only
    the panel, leaving the nav (and its collapsed/expanded state) untouched.
    """
    if view_key not in comercio.VIEW_BY_KEY:
        raise Http404(f"Unknown Mi comercio view: {view_key!r}")

    context = {
        "active_view": view_key,
        "comercio_view": comercio.VIEW_BY_KEY[view_key],
    }
    builder = COMERCIO_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(comercio.panel_template(view_key), context, request=request)
    )


def productos_table(request, tab_key: str):
    """Return just the tabbed table region (tabs + rows) for one Productos tab.

    Targeted by the tab row's HTMX requests so picking a tab re-renders only
    #product-table -- the title and toolbar above it stay put, like the CRM
    pager swapping #client-table.
    """
    if tab_key not in comercio.TAB_BY_KEY:
        raise Http404(f"Unknown Productos tab: {tab_key!r}")

    return HttpResponse(
        render_to_string(
            "partials/comercio/product_table.html",
            {
                "tabs": comercio.TABS,
                "active_tab": tab_key,
                "columns": comercio.TABLE_COLUMNS,
                "products": comercio.get_products(tab_key),
            },
            request=request,
        )
    )


def producto_create(request):
    """Placeholder destination for the "Crear +" button.

    Wired so the button goes somewhere real; the creation flow itself is not
    built yet.
    """
    return HttpResponse(
        render_to_string("partials/comercio/panels/_crear.html", {}, request=request)
    )


def producto_import(request):
    """Placeholder destination for the "Importar" button.

    Wired so the button goes somewhere real; the import flow itself is not
    built yet.
    """
    return HttpResponse(
        render_to_string("partials/comercio/panels/_importar.html", {}, request=request)
    )


def estadisticas_panel(request, view_key: str):
    """Return just the Estadísticas column 3 for one secondary-nav view.

    Targeted by the nav panel's HTMX requests so picking a page re-renders only
    the panel, leaving the nav untouched.
    """
    if view_key not in estadisticas.VIEW_BY_KEY:
        raise Http404(f"Unknown Estadísticas view: {view_key!r}")

    context = {
        "active_view": view_key,
        "stats_view": estadisticas.VIEW_BY_KEY[view_key],
    }
    builder = STATS_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(
            estadisticas.panel_template(view_key), context, request=request
        )
    )


def _volumen_context(request) -> dict:
    """Data for the Volumen de Mensajes detail screen.

    The whole screen is governed by one period, so the range is parsed once
    here and the first render ships its report inline -- the page is useful
    before any JS runs. Changing the picker re-fetches
    :func:`estadisticas_volumen_data` instead of re-rendering the panel.
    """
    start, end = estadisticas_volumen.parse_range(request.GET)
    return {
        "period_start": start,
        "period_end": end,
        "period_label": estadisticas_volumen.format_range(start, end),
        "report": estadisticas_volumen.report(start, end),
        "channels": estadisticas_volumen.CHANNELS,
    }


def _tiempos_context(request) -> dict:
    """Data for the Tiempos de Respuesta detail screen.

    Same stance as :func:`_volumen_context` -- the first render ships its
    report inline; moving any of the three filters re-fetches
    :func:`estadisticas_tiempos_data` -- plus the two new filters' option
    lists (agents and platforms) and their applied values.
    """
    start, end = estadisticas_tiempos.parse_range(request.GET)
    agent = estadisticas_tiempos.parse_agent(request.GET)
    platform = estadisticas_tiempos.parse_platform(request.GET)
    return {
        "period_start": start,
        "period_end": end,
        "period_label": estadisticas_tiempos.format_range(start, end),
        "report": estadisticas_tiempos.report(start, end, agent, platform),
        "agents": get_user_model().objects.filter(is_active=True).order_by("username"),
        "selected_agent": agent.pk if agent else "",
        "platforms": estadisticas_tiempos.PLATFORMS,
        "selected_platform": platform,
    }


#: Stat card key -> callable(request) -> dict, mirroring
#: :data:`STATS_PANEL_CONTEXT`. Cards without an entry render the
#: placeholder and need no data.
STATS_CARD_CONTEXT = {
    "volumen-mensajes": _volumen_context,
    "tiempos-respuesta": _tiempos_context,
}


def estadisticas_card(request, card_key: str):
    """One Mensajería stat card's detail screen.

    Which template renders is resolved by name
    (:func:`estadisticas.card_template`), so the three cards still on the
    placeholder become real screens by adding a template plus a context
    builder above -- no branching here.
    """
    card = estadisticas.CARD_BY_KEY.get(card_key)
    if card is None:
        raise Http404(f"Unknown stat card: {card_key!r}")

    context = {"stat_card": card}
    builder = STATS_CARD_CONTEXT.get(card_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(
            estadisticas.card_template(card_key), context, request=request
        )
    )


def estadisticas_volumen_data(request):
    """JSON feed behind the Volumen de Mensajes screen's period picker.

    Answers the same report the panel renders inline, so moving the picker
    updates the tiles, the chart and the table from one request instead of
    re-rendering (and re-mounting) the whole screen. An unusable range falls
    back to the default window rather than erroring -- the response echoes
    the range it actually used so the picker can correct itself.
    """
    start, end = estadisticas_volumen.parse_range(request.GET)
    return JsonResponse(estadisticas_volumen.report(start, end))


def estadisticas_tiempos_data(request):
    """JSON feed behind the Tiempos de Respuesta screen's filters.

    Same contract as :func:`estadisticas_volumen_data`: one request answers
    the whole report, and unusable filter values fall back (default period,
    all agents, all platforms) with the response echoing what it used.
    """
    start, end = estadisticas_tiempos.parse_range(request.GET)
    agent = estadisticas_tiempos.parse_agent(request.GET)
    platform = estadisticas_tiempos.parse_platform(request.GET)
    return JsonResponse(estadisticas_tiempos.report(start, end, agent, platform))


def mensajeria_panel(request, view_key: str):
    """Return just the Configuración de mensajería column 3 for one
    secondary-nav view.

    Targeted by the nav panel's HTMX requests so picking a page re-renders only
    the panel, leaving the nav untouched.
    """
    if view_key not in mensajeria.VIEW_BY_KEY:
        raise Http404(f"Unknown Mensajería view: {view_key!r}")

    context = {
        "active_view": view_key,
        "msg_view": mensajeria.VIEW_BY_KEY[view_key],
    }
    builder = MENSAJERIA_PANEL_CONTEXT.get(view_key)
    if builder is not None:
        context.update(builder(request))

    return HttpResponse(
        render_to_string(mensajeria.panel_template(view_key), context, request=request)
    )


def plantillas_table(request, tab_key: str):
    """Return just the tabbed table region (tabs + rows) for one Plantillas tab.

    Targeted by the tab row's HTMX requests so picking a tab re-renders only
    #template-table -- the toolbar above it stays put, like the Productos tabs.
    """
    if tab_key not in mensajeria.TAB_BY_KEY:
        raise Http404(f"Unknown Plantillas tab: {tab_key!r}")

    return HttpResponse(
        render_to_string(
            "partials/mensajeria/template_table.html",
            {
                "tabs": mensajeria.TABS,
                "active_tab": tab_key,
                "columns": mensajeria.TABLE_COLUMNS,
                "templates": mensajeria.get_templates(tab_key),
            },
            request=request,
        )
    )


# --- Respuestas rápidas CRUD ------------------------------------------------
#
# Same shape as the Clientes CRUD: one shared modal on the panel
# (#reply-modal-body), each button fetching the body it needs, a successful
# save answering with a fragment that closes the modal and swaps the table
# in out-of-band, a rejected one re-rendering the form with its errors.


def _reply_form_response(request, state, errors, reply=None):
    return HttpResponse(
        render_to_string(
            "partials/mensajeria/respuestas/form.html",
            {
                "form": state,
                "errors": errors,
                "reply": reply,
                "title_max": respuestas.TITLE_MAX,
                "body_max": respuestas.BODY_MAX,
            },
            request=request,
        )
    )


def _reply_saved_response(request, message: str):
    context = _respuestas_context(request)
    context["reply_notice"] = message
    return HttpResponse(
        render_to_string(
            "partials/mensajeria/respuestas/saved.html", context, request=request
        )
    )


def respuesta_form(request, reply_id: int | None = None):
    """The Crear/Editar respuesta rápida dialog: GET renders, POST saves.

    Multipart, because of the image: the form posts with hx-encoding and the
    upload arrives in request.FILES.
    """
    reply = get_object_or_404(QuickReply, pk=reply_id) if reply_id else None

    if request.method == "POST":
        state = respuestas.form_state(request.POST)
        upload = request.FILES.get("image")
        errors = respuestas.validate(state, upload, reply)
        if not errors:
            saved = respuestas.apply(state, upload, reply, request.user)
            return _reply_saved_response(
                request,
                f"Respuesta actualizada: {saved.title}."
                if reply
                else f"Respuesta creada: {saved.title}.",
            )
        return _reply_form_response(request, state, errors, reply)

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _reply_form_response(request, respuestas.form_state(reply=reply), {}, reply)


def respuesta_delete(request, reply_id: int):
    """GET asks; POST deletes the reply.

    The stored image file stays: messages already sent with it point at that
    URL (``Message.media_url``), and deleting the bytes would break real
    history. See core.respuestas.apply for the same reasoning.
    """
    reply = get_object_or_404(QuickReply, pk=reply_id)
    if request.method == "POST":
        title = reply.title
        reply.delete()
        return _reply_saved_response(request, f"Respuesta eliminada: {title}.")
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return HttpResponse(
        render_to_string(
            "partials/mensajeria/respuestas/delete.html", {"reply": reply}, request=request
        )
    )


def respuesta_toggle(request, reply_id: int):
    """Flip a reply's active switch from the table -- hidden from the picker
    without losing the text."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    reply = get_object_or_404(QuickReply, pk=reply_id)
    reply.is_active = request.POST.get("active") == "1"
    reply.save(update_fields=["is_active", "updated_at"])
    return HttpResponse(
        render_to_string(
            "partials/mensajeria/respuestas/table.html",
            _respuestas_context(request),
            request=request,
        )
    )


def plantillas_sync(request):
    """Pull approval verdicts from the provider and re-render the table.

    Behind the "Sincronizar con WhatsApp" button. Meta reviews templates on
    its own clock and the CRM has no webhook for the verdict, so this is how
    Pendiente becomes Aceptada (or Rechazada, with the reason). On a provider
    without a catalogue it says so instead of pretending to have checked.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    provider = messaging_services.get_provider()
    try:
        changed = messaging_services.sync_template_verdicts()
    except Exception as exc:
        notice = f"No se pudo consultar a WhatsApp: {exc}"
    else:
        if type(provider).template_verdicts is MessagingProviderBase.template_verdicts:
            # Inherited the base no-op: there is nothing to consult.
            notice = (
                f"El proveedor activo ({provider.name}) no tiene catálogo de "
                "plantillas que consultar."
            )
        elif changed:
            notice = f"Estados actualizados: {changed} plantilla(s) cambiaron."
        else:
            notice = "Estados al día: ninguna plantilla cambió."

    context = _plantillas_context(request)
    context["plantillas_notice"] = notice
    return HttpResponse(
        render_to_string("partials/mensajeria/template_table.html", context, request=request)
    )


# --- Automations: bienvenida, asignación automática, widget ------------------
#
# Three screens over one settings row (core.models.MessagingSettings). Each
# POSTs its own fields and answers with its own panel re-rendered, so saving
# one never silently rewrites another's. Every one starts switched off.


def _automations_response(request, view_key: str, notice=None, errors=None):
    context = _automations_context(request)
    context.update({"saved_notice": notice, "errors": errors or {}, "active_view": view_key,
                    "msg_view": mensajeria.VIEW_BY_KEY[view_key]})
    return HttpResponse(
        render_to_string(mensajeria.panel_template(view_key), context, request=request)
    )


def bienvenida_save(request):
    """Save the welcome message. Enabling it with no text is refused rather
    than stored: a switch that is on and does nothing is worse than off."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    row = MessagingSettings.load()
    body = (request.POST.get("welcome_body") or "").strip()
    enabled = request.POST.get("welcome_enabled") == "1"

    if enabled and not body:
        return _automations_response(
            request, "mensajes-bienvenida",
            errors={"welcome_body": "Escribe el mensaje antes de activarlo."},
        )
    if len(body) > 1024:
        return _automations_response(
            request, "mensajes-bienvenida",
            errors={"welcome_body": "Máximo 1024 caracteres."},
        )

    row.welcome_body, row.welcome_enabled = body, enabled
    row.save(update_fields=["welcome_body", "welcome_enabled", "updated_at"])
    return _automations_response(
        request, "mensajes-bienvenida",
        notice="Bienvenida activada." if enabled else "Bienvenida desactivada.",
    )


def asignacion_save(request):
    """Turn round-robin assignment on or off.

    Enabling it with no agents configured is refused: it would look active
    and quietly leave every conversation unassigned.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    row = MessagingSettings.load()
    enabled = request.POST.get("assign_enabled") == "1"

    if enabled and not agents.agent_users():
        return _automations_response(
            request, "asignacion-automatica",
            errors={"assign_enabled": "No hay agentes a quienes asignar. Crea uno en CRM › Equipo › Usuarios."},
        )

    row.assign_enabled = enabled
    if not enabled:
        row.assign_cursor = 0   # start the rotation clean next time it is on
    row.save(update_fields=["assign_enabled", "assign_cursor", "updated_at"])
    return _automations_response(
        request, "asignacion-automatica",
        notice="Asignación automática activada." if enabled else "Asignación automática desactivada.",
    )


def widget_save(request):
    """Save the WhatsApp widget's configuration. The phone is normalized the
    same way a client's is, so the wa.me link cannot be built from a number
    the CRM would store differently."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    row = MessagingSettings.load()
    phone = clientes.normalize_phone(request.POST.get("widget_phone") or "")
    errors = {}
    if phone and not 7 <= len(phone) - 1 <= 15:
        errors["widget_phone"] = "Escribe el número con indicativo, p. ej. +57 300 123 4567."
    position = request.POST.get("widget_position") or "right"
    if position not in dict(MessagingSettings.WIDGET_POSITIONS):
        errors["widget_position"] = "Posición desconocida."
    if errors:
        return _automations_response(request, "widget-whatsapp", errors=errors)

    row.widget_phone = phone
    row.widget_greeting = (request.POST.get("widget_greeting") or "").strip()[:140]
    row.widget_label = (request.POST.get("widget_label") or "").strip()[:40] or "Escríbenos"
    row.widget_position = position
    row.save(update_fields=["widget_phone", "widget_greeting", "widget_label",
                            "widget_position", "updated_at"])
    return _automations_response(request, "widget-whatsapp", notice="Widget guardado.")


def _mensajeria_page(request, panel_template: str, panel_context: dict) -> HttpResponse:
    """The full mensajería page (base.html + sidebar + section) with the
    Plantillas panel swapped for ``panel_template``.

    Plain (non-HTMX) navigation lands here -- open-in-new-tab on a chooser
    card, or a no-JS form POST -- so those URLs render inside the shell
    instead of as a bare fragment. Mirrors section().
    """
    item = NAV_BY_KEY["mensajeria"]
    context = {
        "primary_nav": PRIMARY_NAV,
        "secondary_nav": SECONDARY_NAV,
        "active_key": "mensajeria",
        "item": item,
        "page_title": item.label,
        "section_template": _section_template("mensajeria"),
    }
    context.update(_mensajeria_context(request))
    context.update(panel_context)
    context["panel_template"] = panel_template
    return HttpResponse(render_to_string("base.html", context, request=request))


def plantilla_gallery(request):
    """Placeholder destination for the chooser's "Seleccionar plantilla" card.

    The template gallery gets built here later. HTMX gets the bare panel
    fragment; plain navigation gets it inside the full page shell.
    """
    template = "partials/mensajeria/panels/_galeria_plantillas.html"
    if _is_htmx(request):
        return HttpResponse(render_to_string(template, {}, request=request))
    return _mensajeria_page(request, template, {})


def _plantilla_editor_context(state=None, errors=None) -> dict:
    """Everything the Crear plantilla editor renders from: the option lists
    out of core.plantillas, the field values (defaults or a re-render of what
    was posted) and any validation errors."""
    return {
        "categories": plantillas.CATEGORIES,
        "sub_types": plantillas.SUB_TYPES,
        "languages": plantillas.LANGUAGES,
        "header_types": plantillas.HEADER_TYPES,
        "button_kinds": plantillas.BUTTON_KINDS,
        "teams": plantillas.team_options(),
        "form": state or plantillas.form_state(),
        "errors": errors or {},
        "body_max": plantillas.BODY_MAX,
        "header_text_max": plantillas.HEADER_TEXT_MAX,
        "footer_max": plantillas.FOOTER_MAX,
        "button_text_max": plantillas.BUTTON_TEXT_MAX,
    }


def plantilla_editor(request):
    """The Crear plantilla screen behind the chooser's "Empezar a crear" card.

    GET renders the two-column editor (form + live preview). POST validates
    through core.plantillas and, when clean, saves the template as Pendiente:
    HTMX gets the re-rendered Plantillas panel back (the region the editor
    was swapped into) while a plain POST redirects to the list. Errors
    re-render the editor with inline messages, keeping what was typed.
    """
    if request.method == "POST":
        state = plantillas.form_state(request.POST)
        errors = plantillas.validate(state, request.FILES)
        if not errors:
            template = MessageTemplate(**plantillas.model_kwargs(state, request.FILES))
            try:
                # The validator's exists() check can lose a race against a
                # concurrent save of the same (name, language); the unique
                # constraint backs it up, so answer with the same message.
                template.save()
            except IntegrityError:
                if template.header_media:
                    # FileField wrote the upload before the failed INSERT.
                    template.header_media.delete(save=False)
                errors["name"] = "Ya existe una plantilla con este nombre en este idioma."
            else:
                # Saved locally first, submitted second: a Meta hiccup must
                # not cost the editor's work. On a provider without a
                # catalogue (fake) this is a no-op and the
                # plantilla simply stays a local Pendiente record.
                notice = None
                try:
                    messaging_services.submit_template(template)
                except messaging_services.TemplateSubmissionFailed as exc:
                    notice = (
                        f"La plantilla «{template.name}» se guardó, pero WhatsApp "
                        f"no la aceptó para revisión: {exc}"
                    )
                if _is_htmx(request):
                    context = _plantillas_context(request)
                    context["plantillas_notice"] = notice
                    return HttpResponse(
                        render_to_string(
                            "partials/mensajeria/panels/plantillas-whatsapp.html",
                            context,
                            request=request,
                        )
                    )
                return redirect(
                    reverse("section", args=["mensajeria"])
                    + "?view=plantillas-whatsapp"
                )
        context = _plantilla_editor_context(state=state, errors=errors)
    else:
        context = _plantilla_editor_context()

    template_name = "partials/mensajeria/panels/plantilla_editor.html"
    if _is_htmx(request):
        return HttpResponse(render_to_string(template_name, context, request=request))
    return _mensajeria_page(request, template_name, context)


# --- Mi calendario ---------------------------------------------------------


def _calendar_event_from_post(request, event=None):
    """Build or refresh a CalendarEvent from the event modal's POST.

    Returns ``(event, errors)`` -- errors is field -> Spanish message and
    nothing may be saved while it is non-empty. All datetimes go through
    core.calendario.parse_client_dt, so the modal's wall-clock strings land
    in the database as UTC.
    """
    data = request.POST
    errors = {}
    event = event or CalendarEvent()

    title = data.get("title", "").strip()
    if not title:
        errors["title"] = "Escribe un título."
    elif len(title) > 120:
        errors["title"] = "Máximo 120 caracteres."
    event.title = title

    event.description = data.get("description", "").strip()

    event_type = data.get("event_type", calendario.DEFAULT_EVENT_TYPE)
    if event_type in calendario.EVENT_TYPE_BY_KEY:
        event.event_type = event_type
    else:
        errors["event_type"] = "Elige un tipo de la lista."

    event.all_day = data.get("all_day") == "1"
    date_str = data.get("date", "").strip()
    try:
        if event.all_day:
            # Optional end_date makes multi-day all-day events expressible;
            # stored end stays exclusive (last day + 1).
            end_date = data.get("end_date", "").strip() or date_str
            event.start = calendario.parse_client_dt(f"{date_str}T00:00:00")
            event.end = calendario.parse_client_dt(f"{end_date}T00:00:00") + timedelta(
                days=1
            )
        else:
            start_time = data.get("start_time", "").strip()
            end_time = data.get("end_time", "").strip()
            event.start = calendario.parse_client_dt(f"{date_str}T{start_time}")
            event.end = calendario.parse_client_dt(f"{date_str}T{end_time}")
            # An event ending at midnight ends on the NEXT day.
            if event.end <= event.start and end_time == "00:00":
                event.end += timedelta(days=1)
    except (ValueError, OverflowError):
        errors["when"] = "Revisa la fecha y las horas."
    else:
        if event.end <= event.start:
            errors["when"] = (
                "La fecha de fin no puede ser anterior a la de inicio."
                if event.all_day
                else "La hora de fin debe ser posterior a la de inicio."
            )

    contact_id = data.get("contact", "").strip()
    if contact_id:
        try:
            event.contact = Client.objects.get(pk=int(contact_id))
        except (ValueError, Client.DoesNotExist):
            errors["contact"] = "Ese cliente no existe."
    else:
        event.contact = None

    assigned_id = data.get("assigned_to", "").strip()
    if assigned_id:
        try:
            event.assigned_to = get_user_model().objects.get(pk=int(assigned_id))
        except (ValueError, get_user_model().DoesNotExist):
            errors["assigned_to"] = "Ese usuario no existe."
    else:
        event.assigned_to = None

    reminder = data.get("reminder", "").strip()
    if not reminder:
        event.reminder_minutes_before = None
    elif reminder in calendario.REMINDER_KEYS:
        event.reminder_minutes_before = int(reminder)
    else:
        # Off-menu values would be stored but silently lost on the next
        # edit (the select can't show them), so reject them outright.
        errors["reminder"] = "Recordatorio inválido."

    return event, errors


def calendar_events(request):
    """JSON feed for the calendar grid: events overlapping [start, end).

    FullCalendar refetches this on every navigation, so only the visible
    range ever leaves the database -- never the whole table.
    """
    try:
        range_start = calendario.parse_client_dt(request.GET["start"])
        range_end = calendario.parse_client_dt(request.GET["end"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "Rango inválido."}, status=400)
    # FullCalendar's widest legitimate fetch is a padded month (~6 weeks);
    # cap the span so a hand-made range can't dump the whole table.
    if range_end <= range_start or range_end - range_start > timedelta(days=100):
        return JsonResponse({"error": "Rango inválido."}, status=400)

    events = CalendarEvent.objects.filter(
        start__lt=range_end, end__gt=range_start
    ).select_related("contact")
    return JsonResponse(
        [calendario.serialize_event(event) for event in events], safe=False
    )


def calendar_event_create(request):
    """Create an event from the modal. Answers JSON: the serialized event on
    success, field errors (400) otherwise."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    event, errors = _calendar_event_from_post(request)
    if errors:
        return JsonResponse({"errors": errors}, status=400)
    if request.user.is_authenticated:
        event.created_by = request.user
    event.save()
    return JsonResponse({"ok": True, "event": calendario.serialize_event(event)})


def calendar_event_update(request, event_id: int):
    """Full edit of one event from the modal (same payload as create)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    event = get_object_or_404(CalendarEvent, pk=event_id)
    event, errors = _calendar_event_from_post(request, event)
    if errors:
        return JsonResponse({"errors": errors}, status=400)
    event.save()
    return JsonResponse({"ok": True, "event": calendario.serialize_event(event)})


def calendar_event_move(request, event_id: int):
    """Persist a drag or resize: new start/end wall-clock strings (and
    whether the event landed in the all-day lane). The client updates
    optimistically and reverts if this answers anything but ok."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    event = get_object_or_404(CalendarEvent, pk=event_id)
    try:
        start = calendario.parse_client_dt(request.POST["start"])
        end = calendario.parse_client_dt(request.POST["end"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "Fechas inválidas."}, status=400)

    all_day = request.POST.get("all_day") == "1"
    if all_day:
        # A drag into the all-day lane arrives with arbitrary wall clocks
        # (FullCalendar may even drop the end); snap to midnights.
        start, end = calendario.normalize_all_day(start, end)
    elif end < start:
        return JsonResponse({"error": "Rango inválido."}, status=400)
    elif end == start:
        # A drop out of the all-day lane loses its end; give it an hour.
        end = start + timedelta(hours=1)

    event.start = start
    event.end = end
    event.all_day = all_day
    event.save(update_fields=["start", "end", "all_day", "updated_at"])
    return JsonResponse({"ok": True})


def calendar_event_delete(request, event_id: int):
    """Delete one event, from the modal's Eliminar action."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    get_object_or_404(CalendarEvent, pk=event_id).delete()
    return JsonResponse({"ok": True})


def calendar_prefs(request):
    """Persist the sidebar preferences (weekends toggle, slot duration) in
    the session and echo the validated values back."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    calendario.set_prefs(
        request.session,
        weekends=request.POST.get("weekends") == "1",
        slot=request.POST.get("slot", ""),
    )
    return JsonResponse({"ok": True, **calendario.get_prefs(request.session)})


# --- Public legal pages -----------------------------------------------------
# Reachable without a session (see core.middleware.EXEMPT_PATHS). Meta requires
# a publicly readable privacy policy and data-deletion URL before an app can be
# published, and a policy sitting behind the login gate is not a policy anyone
# -- reviewer or customer -- can actually read.

#: Shown as "Last updated" on both pages. A constant rather than today's date:
#: a policy that claims to change every time it is rendered is worthless.
LEGAL_UPDATED = '2 de septiembre de 2026'


def _legal_context():
    return {
        'updated': LEGAL_UPDATED,
        'entity': settings.LEGAL_ENTITY_NAME,
        'contact_email': settings.LEGAL_CONTACT_EMAIL,
    }


def privacy(request):
    """The privacy policy. Public by design."""
    return render(request, 'legal/privacidad.html', _legal_context())


def data_deletion(request):
    """How to ask us to delete your data. Public by design."""
    return render(request, 'legal/eliminacion-de-datos.html', _legal_context())


def stored_file(request, token, filename):
    """Serve a file kept in the database (``core.storage.DatabaseStorage``).

    Deliberately unauthenticated, and exempt from the login gate: WhatsApp
    fetches these URLs from Meta's own servers when the app sends an image,
    so a redirect to /login/ here means the customer never receives the photo.
    What stands in for a login is the token -- 32 random hex characters, the
    only way to address a row -- so the URLs are unguessable rather than open.

    ``filename`` is cosmetic (it gives a saved link a sensible name) and is not
    matched against anything: the token alone identifies the file.
    """
    from core.models import StoredFile

    row = StoredFile.objects.filter(token=token).first()
    if row is None:
        raise Http404("archivo no encontrado")

    response = HttpResponse(
        bytes(row.content), content_type=row.content_type or "application/octet-stream"
    )
    response["Content-Length"] = str(row.size)
    # Immutable: a token addresses one set of bytes for its lifetime, so this
    # keeps Meta and the browser from re-fetching the same photo every time.
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    # These bytes were uploaded by an agent and are served from the app's own
    # origin, unauthenticated. An SVG can carry <script>; opened directly, it
    # would run with the session of whoever clicked the link. The sandbox
    # directive makes anything served here inert when navigated to, and the
    # disposition keeps the browser treating it as a file, not a page.
    # Neither affects <img src=...> or Meta's download.
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=False, filename=posixpath.basename(row.name)
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response
