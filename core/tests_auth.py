"""Tests for the app-wide login gate (core/middleware.py + core.views.login_view).

TESTING (settings.py) makes the gate a no-op for every other test file, so it
doesn't have to log in before touching a view. These tests turn that back off
to exercise the real thing, and pin credentials via override_settings rather
than relying on whatever a developer's local .env happens to hold.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from core.middleware import SESSION_KEY

CREDS = {"username": "tester", "password": "secret-pw"}


# APP_AGENTS="" matters as much as the pair below it: core.agents prefers the
# agent list whenever it is non-empty, so a developer with APP_AGENTS in their
# .env would otherwise never reach the legacy credentials these tests pin.
@override_settings(
    TESTING=False,
    APP_AGENTS="",
    APP_LOGIN_USERNAME="tester",
    APP_LOGIN_PASSWORD="secret-pw",
)
class LoginGateTests(TestCase):
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
        with override_settings(APP_LOGIN_USERNAME="", APP_LOGIN_PASSWORD=""):
            self.client.post(reverse("login"), {"username": "", "password": ""})
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_provider_webhook_is_reachable_without_a_session(self):
        # Signature-authenticated (see messaging/views.py), not session-gated --
        # an unsigned request should 401, never redirect to login.
        #
        # Meta on purpose: this class runs with TESTING=False, i.e. as a real
        # deployment would, and Meta's is the door that really is open in
        # production, so it is the one worth asserting stays
        # signature-gated rather than login-gated.
        response = self.client.post(
            reverse("messaging_webhook", args=["meta"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
