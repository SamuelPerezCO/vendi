"""Tests for the app-wide login gate (core/middleware.py + core.views.login_view).

TESTING (settings.py) makes the gate a no-op for every other test file, so it
doesn't have to log in before touching a view. These tests turn that back off
to exercise the real thing, signing in with an account created in the
database -- the only place logins come from.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from core import agents
from core.middleware import SESSION_KEY

CREDS = {"username": "tester", "password": "secret-pw"}


@override_settings(TESTING=False)
class LoginGateTests(TestCase):
    def setUp(self):
        agents.create_user("tester", "secret-pw", "Tester")

    def test_unauthenticated_request_redirects_to_login(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_unauthenticated_section_redirects_with_next(self):
        url = reverse("section", args=["inbox"])
        response = self.client.get(url)
        self.assertRedirects(
            response, f"{reverse('login')}?next={url}", fetch_redirect_response=False
        )

    def test_login_page_loads(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Iniciar sesión")

    def test_wrong_credentials_show_error_and_do_not_authenticate(self):
        response = self.client.post(
            reverse("login"), {"username": "tester", "password": "nope"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "incorrectos")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_correct_credentials_authenticate_and_redirect_home(self):
        response = self.client.post(reverse("login"), CREDS)
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertTrue(self.client.session.get(SESSION_KEY))

    def test_authenticated_session_can_reach_sections(self):
        self.client.post(reverse("login"), CREDS)
        response = self.client.get(reverse("section", args=["inbox"]))
        self.assertEqual(response.status_code, 200)

    def test_next_param_redirects_there_on_success(self):
        target = reverse("section", args=["crm"])
        response = self.client.post(f"{reverse('login')}?next={target}", CREDS)
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_unsafe_next_param_falls_back_to_home(self):
        """A crafted ?next=https://evil.example would otherwise redirect a
        successful login straight off the site -- the classic post-login
        open-redirect phishing setup."""
        response = self.client.post(
            f"{reverse('login')}?next=https://evil.example/phish", CREDS
        )
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_logout_clears_session_and_locks_again(self):
        self.client.post(reverse("login"), CREDS)
        self.client.get(reverse("logout"))
        self.assertNotIn(SESSION_KEY, self.client.session)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_blank_credentials_never_satisfy_the_gate(self):
        self.client.post(reverse("login"), {"username": "", "password": ""})
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_the_retired_env_logins_open_nothing(self):
        """APP_LOGIN_* once let a pair from the environment in, and APP_AGENTS
        seeded accounts. Set or not, only database accounts sign in now."""
        from unittest import mock

        env = {
            "APP_AGENTS": "Admin:admin-pw:Admin",
            "APP_LOGIN_USERNAME": "viejo",
            "APP_LOGIN_PASSWORD": "clave-vieja",
        }
        with mock.patch.dict("os.environ", env), override_settings(**env):
            for username, password in (("viejo", "clave-vieja"), ("Admin", "admin-pw")):
                with self.subTest(username):
                    self.client.post(
                        reverse("login"), {"username": username, "password": password}
                    )
                    self.assertNotIn(SESSION_KEY, self.client.session)

    def test_provider_webhook_is_reachable_without_a_session(self):
        # Signature-authenticated (see messaging/views.py), not session-gated --
        # an unsigned request should 401, never redirect to login.
        #
        # Meta rather than the fake provider on purpose: this class runs with
        # TESTING=False, i.e. as a real deployment would, and there the
        # simulator's webhook is switched off entirely
        # (messaging.providers.registry.webhook_enabled). Meta's is the door
        # that really is open in production, so it is the one worth asserting
        # stays signature-gated rather than login-gated.
        response = self.client.post(
            reverse("messaging_webhook", args=["meta"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
