"""The people who answer conversations -- "agentes" in the UI.

An agent is a login *and* an assignee: the same identity that gets past the
gate in :mod:`core.middleware` is the one a conversation can be assigned to in
the Inbox. Every one of them is a ``django.contrib.auth`` ``User`` row with a
real, usable password -- **the database is the only source of truth** for who
can log in, what their password is, whether they are a master and whether
they are still active. CRM > Equipo > Usuarios is where a master manages all
of that, with no redeploy.

**The first master.** A fresh database holds nobody, and the Usuarios page
needs a master to open it. ``manage.py crear_maestro <usuario>`` creates one
directly in the database, or resets and restores an existing one when no
master can sign in. Nothing about users is read from the environment:
``APP_AGENTS`` and ``APP_LOGIN_USERNAME``/``APP_LOGIN_PASSWORD``, which seeded
masters in earlier versions, are ignored, and :mod:`core.checks` warns
(``core.W004``) while any of them is still set.

**Masters** are the users in the "Maestros" group (:func:`is_master`), plus
any Django superuser; only they manage users. The last master who can
actually log in can never be demoted or deactivated (:class:`LastMaster`), or
the team could lock itself out with nobody able to fix it.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX, make_password
from django.db.models import Q
from django.db.models.functions import Lower


def authenticate(username: str, password: str):
    """Return the ``User`` these credentials belong to, or ``None``.

    The database alone decides: an active row with a usable password,
    checked by Django's own hasher. A deactivated user is turned away, and so
    is a Django admin account -- /admin is a different door (see
    :func:`_is_app_user`).

    Exactly one password verification runs per call: the matched user's, or
    a throwaway of equal cost when nothing matched (the trick ``ModelBackend``
    uses), so a hit and a miss take the same time and leak nothing about
    which usernames exist.
    """
    if not username or not password:
        return None
    User = get_user_model()
    user = User.objects.filter(username=username, is_active=True).first()
    if user is None or not _is_app_user(user):
        make_password(password)   # equal-cost miss; result discarded
        return None
    if not user.check_password(password):
        return None
    return user


#: Floor for a password this app sets, wherever it is set from.
MIN_PASSWORD_LENGTH = 8


class WeakPassword(Exception):
    """The password does not clear :func:`validate_password`'s floor."""


def validate_password(password: str, username: str = "") -> None:
    """A small, Spanish-worded floor -- the project's AUTH_PASSWORD_VALIDATORS
    would say the same things in English, in an all-Spanish UI.

    Public because the Usuarios dialog, ``manage.py crear_maestro`` and
    ``manage.py hashear_clave`` apply the same rule: a password reaching the
    database by any road should clear the same bar.
    """
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres."
        )
    if password.isdigit():
        raise WeakPassword("La contraseña no puede ser solo números.")
    if username and password.casefold() == username.casefold():
        raise WeakPassword("La contraseña no puede ser igual al usuario.")


def _is_app_user(user) -> bool:
    """A row this app's Usuarios page owns: a real, usable password, and not
    a Django staff account.

    Rows made without a password (an old demo fixture, a script's) are not
    teammates. ``is_staff`` is the load-bearing part: it means "may open
    /admin/", a door this CRM does not manage. Listing such a row here would
    let a CRM master reset its password and walk into the Django admin, which
    is a bigger key than the page grants.
    """
    if user.is_staff or user.is_superuser:
        return False
    return bool(user.password) and user.has_usable_password()


def agent_users() -> list:
    """The ``User`` rows for every agent: everyone active with a real
    password, by display name.

    This is what fills the Inbox's assignment dropdown, so it must list
    teammates who have never logged in yet -- an agent you can't assign work to
    until they show up would defeat the point.
    """
    User = get_user_model()
    return [
        user
        for user in User.objects.filter(is_active=True).order_by(
            Lower("first_name"), "username"
        )
        if _is_app_user(user)
    ]


def assignment_options(conversation) -> list:
    """The dropdown options for one conversation: every agent, plus whoever
    it is currently assigned to if they are no longer one.

    That last part is the point. An agent can be deactivated (or be assigned
    from /admin, or by the automation writing into the database) while their
    conversations stay assigned to them; without an option for them the
    ``<select>`` would fall back to its first entry and quietly claim the chat
    is "Sin asignar". Showing the real assignee -- reassignable, but not
    misrepresented -- is the honest rendering.
    """
    options = agent_users()
    current = conversation.assigned_to
    if current is not None and not any(user.pk == current.pk for user in options):
        options.append(current)
    return options


# --- Team management (CRM > Equipo > Usuarios) ------------------------------


#: Django group carrying the master role. A group rather than ``is_staff``
#: on purpose: ``is_staff`` means "may open /admin/", a different question
#: from "may manage this CRM's team" -- the seed marks its demo advisor
#: staff for /admin access, and that must not make them a master here.
#: Built-in model, so no migration of our own.
MASTER_GROUP = "Maestros"


def is_master(user) -> bool:
    """Whether ``user`` may manage the team: a Django superuser, or a user
    in the Maestros group."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=MASTER_GROUP).exists()


def is_app_user(user) -> bool:
    """Public face of :func:`_is_app_user` for the Usuarios page."""
    return _is_app_user(user)


class UsernameTaken(Exception):
    """Another user already has this username."""


def create_user(username: str, password: str, display_name: str = "", master: bool = False):
    """Create a teammate who can log in with ``password``.

    Raises :class:`UsernameTaken` when the name is taken in any letter case,
    so "Lucia" can't be created next to "lucia".
    """
    User = get_user_model()
    username = username.strip()
    if User.objects.filter(username__iexact=username).exists():
        raise UsernameTaken(f"Ya existe un usuario llamado «{username}».")
    user = User(username=username, first_name=(display_name or username)[:150])
    user.set_password(password)
    user.save()
    _set_master(user, master)
    return user


def _set_master(user, master: bool) -> None:
    """Put the user in (or out of) the Maestros group, creating it on first
    use so a fresh deployment needs no fixture."""
    from django.contrib.auth.models import Group

    group, _ = Group.objects.get_or_create(name=MASTER_GROUP)
    if master:
        user.groups.add(group)
    else:
        user.groups.remove(group)


def update_user(user, display_name: str, master: bool, password: str = ""):
    """Rename, promote/demote and optionally reset the password of a user."""
    if not master:
        _guard_last_master(user)
    user.first_name = (display_name or user.username)[:150]
    fields = ["first_name"]
    if password:
        user.set_password(password)
        fields.append("password")
    user.save(update_fields=fields)
    _set_master(user, master)
    return user


def _master_count(exclude_pk=None) -> int:
    """How many masters able to log in would remain.

    A master on paper with no usable password (a row left behind by a
    script, or deactivated) cannot log in, and counting them as the
    survivor would let the last real master go and lock the team out.
    """
    User = get_user_model()
    masters = (
        User.objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(groups__name=MASTER_GROUP))
        .exclude(password="")
        .exclude(password__startswith=UNUSABLE_PASSWORD_PREFIX)
    )
    if exclude_pk is not None:
        masters = masters.exclude(pk=exclude_pk)
    return masters.distinct().count()


class LastMaster(Exception):
    """Refused: the change would leave nobody able to manage the team."""


def _guard_last_master(user) -> None:
    """Refuse a demotion/deactivation that removes the final master."""
    if not is_master(user):
        return
    if _master_count(exclude_pk=user.pk) == 0:
        raise LastMaster(
            "Es el único usuario maestro: nombra a otro antes de quitarle el rol "
            "o desactivarlo."
        )


def set_user_active(user, active: bool):
    """Deactivate (or restore) a user. Deactivating is the only "delete":
    their conversations, messages and events keep pointing at them, they
    just can't log in or be assigned anything new."""
    if not active:
        _guard_last_master(user)
    user.is_active = active
    user.save(update_fields=["is_active"])
    if not active:
        end_sessions(user)
    return user


def end_sessions(user) -> int:
    """Drop every live session belonging to ``user``; returns how many.

    Deactivating a row only stops the *next* login unless the sessions it
    already has are cleared -- otherwise someone just locked out keeps
    browsing until their cookie expires.
    """
    from django.contrib.sessions.models import Session
    from django.utils import timezone

    ended = 0
    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        if str(session.get_decoded().get("_auth_user_id", "")) == str(user.pk):
            session.delete()
            ended += 1
    return ended
