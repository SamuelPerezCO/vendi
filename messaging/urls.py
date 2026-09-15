"""URL map for the messaging app: just the provider webhook endpoint.

Mounted under ``webhooks/messaging/`` in config.urls, giving
``/webhooks/messaging/meta/``.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("<slug:provider_name>/", views.webhook, name="messaging_webhook"),
]
