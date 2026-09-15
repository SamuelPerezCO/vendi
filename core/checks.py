"""System checks for the app's own configuration.

Run by ``manage.py check``, and by ``runserver`` on every start, so an
misconfigured environment says so out loud rather than waiting to be
noticed.
"""

import os

from django.core.checks import Warning, register


#: Environment variables that seeded logins before every account moved into
#: the database. Nothing reads them any more.
RETIRED_LOGIN_ENV_VARS = ("APP_AGENTS", "APP_LOGIN_USERNAME", "APP_LOGIN_PASSWORD")


@register()
def retired_login_env_vars(app_configs, **kwargs):
    """Warn while any retired login variable is still set (core.W004).

    Logins live in the database (core/agents.py) and the CRM no longer reads
    these. A leftover one is dead weight at best, and at worst a password
    hash -- or an old raw password -- sitting in the hosting dashboard for
    anyone who can open it. Only the names are reported, never the values.
    Reads the process environment directly, because settings no longer
    define them; no database, so it is safe before migrations and in CI.
    """
    leftover = [name for name in RETIRED_LOGIN_ENV_VARS if os.environ.get(name, "").strip()]
    if not leftover:
        return []
    return [
        Warning(
            f"Retired login variables are still set: {', '.join(leftover)}.",
            hint=(
                "Logins live in the database now and the CRM ignores these. "
                "Delete them from the environment (the Vercel project settings, "
                "a local .env). If nobody can sign in, `manage.py crear_maestro "
                "<usuario>` creates or restores a master."
            ),
            id="core.W004",
        )
    ]


@register()
def public_origin_unresolved(app_configs, **kwargs):
    """Warn when a deployed app has no public origin to hand out (core.W002),
    or has one that WhatsApp could not fetch from anyway (core.W003).

    ``PUBLIC_BASE_URL`` is what goes into the image link WhatsApp is given
    for a quick reply; Meta fetches it from its own servers. Empty, the link
    falls back to the request's Host -- which on Vercel is whichever alias
    the agent logged in through, and every alias but the production domain
    sits behind SSO. The result is a message that is accepted and then fails
    a few seconds later with nothing in the app to say why. Surfacing the
    gap at build time (migrate runs the checks) beats discovering it from a
    red tick in the thread.

    "Deployed" is VERCEL being set, or DEBUG being off. Not DEBUG alone: a
    Vercel environment that forgot DEBUG=False would otherwise silence the
    one check meant for it. Quiet under test.
    """
    import os
    from urllib.parse import urlsplit

    from django.conf import settings

    if getattr(settings, "TESTING", False):
        return []
    if settings.DEBUG and not os.environ.get("VERCEL"):
        return []

    origin = getattr(settings, "PUBLIC_BASE_URL", "")
    if not origin:
        return [
            Warning(
                "PUBLIC_BASE_URL is empty: no public origin for the image links "
                "handed to WhatsApp.",
                hint=(
                    "Set PUBLIC_BASE_URL to the one publicly reachable origin "
                    "(the production domain, e.g. https://mvp-crm-lake.vercel.app). "
                    "Without it the link is built from the request's Host, and on "
                    "Vercel every alias except the production domain is behind SSO "
                    "-- Meta gets a login page instead of the photo, and the send "
                    "fails after the fact."
                ),
                id="core.W002",
            )
        ]

    parts = urlsplit(origin)
    protected = set(getattr(settings, "VERCEL_PROTECTED_ALIASES", []))
    if os.environ.get("VERCEL_URL"):
        protected.add(os.environ["VERCEL_URL"])   # per-deployment URL, also SSO
    problems = []
    if parts.scheme != "https":
        problems.append(f"scheme is {parts.scheme!r}, not https")
    if not parts.hostname or parts.hostname.startswith((".", "*")):
        problems.append(f"host {parts.hostname!r} is not a single public host")
    if parts.path or parts.query or parts.fragment:
        problems.append("it carries a path or query; it must be a bare origin")
    if parts.hostname in protected:
        problems.append(f"{parts.hostname} is behind Vercel SSO")
    if not problems:
        return []
    return [
        Warning(
            f"PUBLIC_BASE_URL={origin!r} is not an origin WhatsApp can fetch from: "
            + "; ".join(problems) + ".",
            hint=(
                "It must be https://<the production domain> and nothing else -- "
                "no path, and not one of the SSO-protected aliases (VERCEL_URL, "
                "the team alias, the git-branch alias). Meta downloads image "
                "links from its own servers with no session; anything that "
                "redirects to a login page fails the send after the fact."
            ),
            id="core.W003",
        )
    ]
