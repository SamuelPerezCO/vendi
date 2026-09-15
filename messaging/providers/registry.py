"""Which provider is live, decided by settings alone.

Meta's Cloud API is the only provider the app has, and ``MESSAGING_PROVIDER``
(env var / settings) still names it explicitly; no code imports a concrete
provider class except this module and the tests. Under ``manage.py test`` a
test-only stub joins the table (see ``messaging/testing.py``).
"""

from __future__ import annotations

from django.conf import settings

from .base import MessagingProvider
from .meta import MetaProvider

_PROVIDERS: dict[str, type[MessagingProvider]] = {
    MetaProvider.name: MetaProvider,
}


def get_provider(name: str | None = None) -> MessagingProvider:
    """The active provider (``settings.MESSAGING_PROVIDER``), or a named one.

    The explicit ``name`` form exists for the webhook URL, which addresses a
    provider by slug: a status callback must be parsed by the provider that
    sent it even if the app is mid-migration to another one.
    """
    key = name or settings.MESSAGING_PROVIDER
    try:
        return _PROVIDERS[key]()
    except KeyError:
        raise ValueError(
            f"Unknown messaging provider {key!r}; expected one of {sorted(_PROVIDERS)}"
        ) from None


def is_known_provider(name: str) -> bool:
    """Whether ``name`` is a provider this app has. The webhook answers 404
    for anything else -- including ``fake``, the retired simulator whose
    endpoint turned any request body into customers."""
    return name in _PROVIDERS
