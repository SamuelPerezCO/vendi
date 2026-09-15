"""What the settings module resolves to in each environment it runs in.

Two production failures live here, both of which came from a variable nobody
set rather than from code anyone wrote:

* The CRM answered ``DisallowedHost`` on its own domain. A custom domain
  appears in no ``VERCEL_*`` variable, so it has to be named somewhere, and a
  dashboard env var is somewhere everyone forgets.
* That failure rendered as Django's yellow traceback page -- settings, paths
  and all -- which is only shown when ``DEBUG`` is on. ``DEBUG`` defaulted to
  ``True``, so a deployment that never set it ran in debug *and* silently
  turned off every secure-cookie setting derived from it.

Settings are evaluated at import, so these tests re-evaluate the module in a
throwaway namespace under a chosen environment. That is deliberate: it tests
the file the deployment actually runs, not a re-implementation of its rules,
and it never touches the settings the test suite itself is running under.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

SETTINGS_FILE = Path(settings.BASE_DIR) / "config" / "settings.py"

#: The minimum a deployment must supply for the module to import at all --
#: MESSAGING_PROVIDER is required (there is no silent default).
MINIMUM_ENV = {"MESSAGING_PROVIDER": "meta", "SECRET_KEY": "x" * 50}


def resolve(**env) -> dict:
    """config/settings.py evaluated under ``env`` and nothing else.

    ``clear=True`` so a developer's own exported variables cannot make a test
    pass here and fail on the deployment.
    """
    namespace = {"__file__": str(SETTINGS_FILE), "__name__": "config._under_test"}
    # argv too: TESTING is read from it, and it is true right now because the
    # test runner is what is running. A deployment serves requests through
    # wsgi, so leaving argv alone would resolve settings for a process that
    # does not exist -- and would hide SECURE_SSL_REDIRECT, which TESTING
    # suppresses on purpose.
    with patch.dict(os.environ, {**MINIMUM_ENV, **env}, clear=True), \
            patch.object(sys, "argv", ["vercel_app.py"]):
        exec(compile(SETTINGS_FILE.read_text(encoding="utf-8"), str(SETTINGS_FILE), "exec"),
             namespace)
    return namespace


class CustomDomainTests(SimpleTestCase):
    """The domain the CRM is actually reached at."""

    def test_the_domain_is_allowed_without_any_env_var(self):
        resolved = resolve(VERCEL="1")

        self.assertIn("vendi.lat", resolved["ALLOWED_HOSTS"])
        self.assertIn("www.vendi.lat", resolved["ALLOWED_HOSTS"])

    def test_forms_on_the_domain_can_submit(self):
        # CSRF_TRUSTED_ORIGINS is derived from ALLOWED_HOSTS, and Django needs
        # the scheme-qualified origin or every POST is rejected.
        origins = resolve(VERCEL="1")["CSRF_TRUSTED_ORIGINS"]

        self.assertIn("https://vendi.lat", origins)
        self.assertIn("https://www.vendi.lat", origins)

    def test_an_extra_domain_can_still_be_added_by_env_var(self):
        resolved = resolve(VERCEL="1", ALLOWED_HOSTS="otro.example,mas.example")

        for host in ["otro.example", "mas.example", "vendi.lat"]:
            with self.subTest(host=host):
                self.assertIn(host, resolved["ALLOWED_HOSTS"])

    def test_a_local_checkout_keeps_an_empty_list(self):
        # Django only falls back to localhost/127.0.0.1 while ALLOWED_HOSTS is
        # empty, so adding the domains unconditionally would lock the dev
        # server out of its own machine.
        self.assertEqual(resolve()["ALLOWED_HOSTS"], [])


class DebugDefaultTests(SimpleTestCase):
    """A deployment has to opt *in* to debug, not remember to opt out."""

    def test_a_deployment_that_never_set_debug_is_not_in_debug(self):
        self.assertFalse(resolve(VERCEL="1")["DEBUG"])

    def test_a_local_checkout_still_defaults_to_debug(self):
        self.assertTrue(resolve()["DEBUG"])

    def test_debug_can_still_be_switched_on_deliberately(self):
        self.assertTrue(resolve(VERCEL="1", DEBUG="True")["DEBUG"])

    def test_the_secure_settings_derived_from_debug_follow_it(self):
        # These three are what made the wrong default more than cosmetic: a
        # deployment in debug served session and CSRF cookies without the
        # Secure flag and did not redirect HTTP.
        deployed = resolve(VERCEL="1")

        self.assertTrue(deployed["SESSION_COOKIE_SECURE"])
        self.assertTrue(deployed["CSRF_COOKIE_SECURE"])
        self.assertTrue(deployed["SECURE_SSL_REDIRECT"])

    def test_a_local_checkout_does_not_force_https(self):
        # The same three, off, so runserver works over plain HTTP.
        local = resolve()

        self.assertFalse(local["SESSION_COOKIE_SECURE"])
        self.assertFalse(local["SECURE_SSL_REDIRECT"])


class PublicOriginTests(SimpleTestCase):
    """What goes into the image links WhatsApp fetches."""

    def test_a_deployment_hands_out_the_project_s_own_domain(self):
        self.assertEqual(resolve(VERCEL="1")["PUBLIC_BASE_URL"], "https://www.vendi.lat")

    def test_it_is_the_host_that_serves_directly_not_the_apex(self):
        # The apex 308-redirects to www. Meta fetches these from its own
        # servers, so the origin handed out must not depend on a redirect.
        self.assertNotEqual(resolve(VERCEL="1")["PUBLIC_BASE_URL"], "https://vendi.lat")

    def test_it_no_longer_follows_whatever_vercel_calls_production(self):
        # VERCEL_PROJECT_PRODUCTION_URL moved when the custom domain was
        # assigned, leaving the previous value answering 400 to Meta.
        resolved = resolve(VERCEL="1", VERCEL_PROJECT_PRODUCTION_URL="algo-nuevo.vercel.app")

        self.assertEqual(resolved["PUBLIC_BASE_URL"], "https://www.vendi.lat")

    def test_an_explicit_setting_still_wins(self):
        resolved = resolve(VERCEL="1", PUBLIC_BASE_URL="https://otro.example")

        self.assertEqual(resolved["PUBLIC_BASE_URL"], "https://otro.example")

    def test_a_local_checkout_gets_no_origin(self):
        # Empty is what makes core.W002 fire on a deployment; a local checkout
        # has no public origin and should not pretend otherwise.
        self.assertEqual(resolve()["PUBLIC_BASE_URL"], "")


class LegacyAliasTests(SimpleTestCase):
    def test_the_old_production_url_still_answers(self):
        # Links to it are already out in the world; it began returning 400 on
        # every path when the custom domain took over as production.
        self.assertIn("mvp-crm-lake.vercel.app", resolve(VERCEL="1")["ALLOWED_HOSTS"])

    def test_it_is_not_filed_as_an_sso_protected_alias(self):
        # core.W003 reads that list to decide what must never be handed to
        # WhatsApp, and this host reaches Django without SSO.
        resolved = resolve(VERCEL="1")

        self.assertNotIn("mvp-crm-lake.vercel.app", resolved["VERCEL_PROTECTED_ALIASES"])


class MessagingProviderTests(SimpleTestCase):
    """Meta is the only provider the app has, and every environment must name
    it: anything else refuses to start rather than boot into a CRM whose
    every send and webhook fails."""

    def test_meta_is_accepted(self):
        self.assertEqual(resolve()["MESSAGING_PROVIDER"], "meta")

    def test_a_leftover_fake_refuses_to_start(self):
        # The simulator is gone; a .env or dashboard still naming it lands here.
        with self.assertRaises(ImproperlyConfigured):
            resolve(MESSAGING_PROVIDER="fake")

    def test_the_test_stub_cannot_be_selected_outside_tests(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve(MESSAGING_PROVIDER="stub")

    def test_a_missing_variable_refuses_to_start(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve(MESSAGING_PROVIDER="")
