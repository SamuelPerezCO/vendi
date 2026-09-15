"""App-wide login gate.

Accounts are database users (core/agents.py), checked in core.views.login_view,
which starts a django.contrib.auth session and sets a flag in it. There is no
signup or self-service password reset. This middleware is what enforces it: any
request without that flag is redirected to the login page, except for the
handful of paths that must stay reachable without a browser session --
provider webhooks (hit by Meta, authenticated by
their own signature check instead), the public legal pages, static/media
assets, and Django admin (gated separately by django.contrib.auth).
"""

from django.shortcuts import redirect
from django.urls import reverse

from django.conf import settings

SESSION_KEY = 'app_authenticated'

# /privacidad/ and /eliminacion-de-datos/ are public on purpose: Meta's app
# review reads them without an account, and so must any customer.
EXEMPT_PATHS = {'/login/', '/privacidad/', '/eliminacion-de-datos/'}
EXEMPT_PREFIXES = (
    '/webhooks/', '/static/', '/media/', '/admin/',
    # Database-held uploads. Open for the same reason /media/ is:
    # WhatsApp fetches image URLs from Meta's servers, and a login
    # redirect there means the customer never gets the photo. The
    # random token in the path is what protects these, not the session.
    '/archivos/',
)


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.TESTING or self._is_exempt(request.path):
            return self.get_response(request)

        # Two things have to hold. The session must carry the flag login_view
        # sets, and django.contrib.auth must still resolve it to a real user:
        # AuthenticationMiddleware hands back AnonymousUser for an account
        # deleted or deactivated since the session was created. So a master
        # deactivating someone locks them out on their very next request,
        # flag or no flag -- and the dead session is flushed so their next
        # login starts clean.
        if request.session.get(SESSION_KEY):
            if request.user.is_authenticated:
                return self.get_response(request)
            request.session.flush()
        login_url = reverse('login')
        if request.path != '/':
            login_url = f'{login_url}?next={request.path}'
        return redirect(login_url)

    def _is_exempt(self, path):
        return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)
