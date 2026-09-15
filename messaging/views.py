"""The webhook endpoint providers deliver to.

One URL per provider -- ``/webhooks/messaging/<provider>/`` -- so a
callback is always parsed by the provider that sent it regardless of which one is active
for *sending* (relevant mid-migration).

Contract with providers:

* Verify the signature before trusting a byte of the body; forgeries get 401.
* After that, answer **200 no matter what**. Providers treat non-200 as
  "retry, aggressively"; a payload we can't process would otherwise come
  back in a storm. Failures are logged instead.
* Do the minimum inline: parsing + :func:`services.process_inbound_events`.
  The heavy half already lives in services so it can move to a task queue
  later -- this view would enqueue instead of call.
"""

from __future__ import annotations

import logging

from django.http import Http404, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import services
from .providers.registry import get_provider, is_known_provider

logger = logging.getLogger(__name__)


@csrf_exempt  # signed by the provider, not a browser session
@require_http_methods(["GET", "POST"])
def webhook(request, provider_name: str):
    """Receive one provider webhook (POST) or a verification handshake (GET)."""
    if not is_known_provider(provider_name):
        # Not a provider this app has -- including the retired fake simulator,
        # whose URL must never write rows again. A misconfigured URL from the
        # caller's side, and 404 is the honest answer: here, it does not exist.
        raise Http404(f"Unknown messaging provider: {provider_name!r}")
    provider = get_provider(provider_name)

    if request.method == "GET":
        # Meta's subscribe handshake: echo hub.challenge when the provider
        # accepts the request; anything else gets a 403.
        challenge = provider.handshake(request)
        if challenge is None:
            return HttpResponse("verification failed", status=403)
        # text/plain: the challenge is caller-supplied, and reflecting it as
        # HTML would be a scripting hole on the deployment's own origin.
        return HttpResponse(challenge, content_type="text/plain; charset=utf-8")

    if not provider.verify_signature(request):
        logger.warning("rejected %s webhook: bad signature", provider_name)
        return HttpResponse("invalid signature", status=401)

    try:
        events = provider.parse_webhook(request)
    except ValueError:
        # Unparseable but authentic. Log it and answer 200 -- a non-200 only
        # makes the provider re-send the same unparseable payload.
        logger.exception("unparseable %s webhook payload", provider_name)
        return HttpResponse("ignored")

    services.process_inbound_events(events)  # logs per-event failures itself
    return HttpResponse("ok")
