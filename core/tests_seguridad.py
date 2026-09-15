"""Tests for the login gate's harder edges: the non-ASCII crash, the retired
environment logins, password fingerprints, and the doors a Usuarios master
must not be able to open."""

from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core import agents
from core.checks import retired_login_env_vars
from core.middleware import SESSION_KEY

User = get_user_model()


class NonAsciiLoginTests(TestCase):
    """A login as "José" once reached ``hmac.compare_digest`` on a str, which
    raises TypeError for any non-ASCII character -- a 500 before anything was
    checked. The edge still deserves a pin."""

    def test_a_non_ascii_username_is_rejected_not_a_crash(self):
        agents.create_user("Admin", "clave-larga", "Admin", master=True)
        self.assertIsNone(agents.authenticate("José", "clave-larga"))
        self.assertIsNone(agents.authenticate("Ünïcödé", "x"))

    def test_a_non_ascii_username_and_password_can_log_in(self):
        agents.create_user("José", "contraseña-ñ", "José")
        self.assertIsNotNone(agents.authenticate("José", "contraseña-ñ"))
        self.assertIsNone(agents.authenticate("José", "contrasena-n"))


class PublicOriginWarningTests(TestCase):
    """core.W002: a deployed app with no public origin for WhatsApp links."""

    @override_settings(DEBUG=False, TESTING=False, PUBLIC_BASE_URL="")
    def test_a_deployed_app_with_no_origin_is_warned(self):
        from core.checks import public_origin_unresolved

        [warning] = public_origin_unresolved(None)
        self.assertEqual(warning.id, "core.W002")
        self.assertIn("PUBLIC_BASE_URL", warning.msg)
        self.assertIn("SSO", warning.hint)

    @override_settings(DEBUG=False, TESTING=False, PUBLIC_BASE_URL="https://crm.example.com")
    def test_a_configured_origin_is_quiet(self):
        from core.checks import public_origin_unresolved

        self.assertEqual(public_origin_unresolved(None), [])

    @override_settings(DEBUG=True, TESTING=False, PUBLIC_BASE_URL="")
    def test_development_is_quiet(self):
        # No production domain exists locally, and the fake provider never
        # fetches a link; a warning here would only train people to ignore it.
        from unittest import mock

        from core.checks import public_origin_unresolved

        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("VERCEL", None)
            self.assertEqual(public_origin_unresolved(None), [])

    @override_settings(DEBUG=True, TESTING=False, PUBLIC_BASE_URL="")
    def test_a_vercel_build_that_forgot_debug_off_is_still_warned(self):
        # DEBUG alone must not silence the one check meant for the platform.
        from unittest import mock

        from core.checks import public_origin_unresolved

        with mock.patch.dict("os.environ", {"VERCEL": "1"}):
            [warning] = public_origin_unresolved(None)
        self.assertEqual(warning.id, "core.W002")

    @override_settings(
        DEBUG=False, TESTING=False,
        VERCEL_PROTECTED_ALIASES=["team-alias.vercel.app"],
    )
    def test_an_origin_whatsapp_cannot_fetch_from_is_named(self):
        from unittest import mock

        from core.checks import public_origin_unresolved

        cases = {
            "https://team-alias.vercel.app": "behind Vercel SSO",
            "http://crm.example.com": "not https",
            "https://crm.example.com/app": "path or query",
            "https://*": "not a single public host",
        }
        for origin, expected in cases.items():
            with self.subTest(origin):
                with override_settings(PUBLIC_BASE_URL=origin), mock.patch.dict(
                    "os.environ", {"VERCEL_URL": "mvp-abc123.vercel.app"}
                ):
                    [warning] = public_origin_unresolved(None)
                self.assertEqual(warning.id, "core.W003")
                self.assertIn(expected, warning.msg)

    @override_settings(DEBUG=False, TESTING=False, PUBLIC_BASE_URL="https://mvp-abc123.vercel.app")
    def test_the_per_deployment_url_counts_as_protected(self):
        from unittest import mock

        from core.checks import public_origin_unresolved

        with mock.patch.dict("os.environ", {"VERCEL_URL": "mvp-abc123.vercel.app"}):
            [warning] = public_origin_unresolved(None)
        self.assertEqual(warning.id, "core.W003")


class RetiredLoginEnvWarningTests(TestCase):
    """core.W004: the environment variables that used to seed logins."""

    RETIRED = {"APP_AGENTS": "", "APP_LOGIN_USERNAME": "", "APP_LOGIN_PASSWORD": ""}

    def run_check(self, **env):
        with mock.patch.dict("os.environ", {**self.RETIRED, **env}):
            return retired_login_env_vars(None)

    def test_quiet_when_none_is_set(self):
        self.assertEqual(self.run_check(), [])

    def test_names_every_leftover_variable(self):
        [warning] = self.run_check(
            APP_AGENTS="Admin:pbkdf2_sha256$x:Admin", APP_LOGIN_PASSWORD="vieja"
        )
        self.assertEqual(warning.id, "core.W004")
        self.assertIn("APP_AGENTS", warning.msg)
        self.assertIn("APP_LOGIN_PASSWORD", warning.msg)
        self.assertNotIn("APP_LOGIN_USERNAME", warning.msg)
        self.assertIn("crear_maestro", warning.hint)

    def test_never_shows_the_values(self):
        [warning] = self.run_check(APP_LOGIN_PASSWORD="clave-secreta-vieja")
        self.assertNotIn("clave-secreta-vieja", warning.msg + warning.hint)

    def test_a_blank_variable_does_not_count(self):
        self.assertEqual(self.run_check(APP_AGENTS="   "), [])

    def test_the_variables_open_no_account(self):
        """Set in the environment and in settings alike, they create nobody
        and let nobody in."""
        env = {
            "APP_AGENTS": "Admin:admin-pw:Admin",
            "APP_LOGIN_USERNAME": "viejo",
            "APP_LOGIN_PASSWORD": "clave-vieja",
        }
        with mock.patch.dict("os.environ", env), override_settings(**env):
            self.assertIsNone(agents.authenticate("Admin", "admin-pw"))
            self.assertIsNone(agents.authenticate("viejo", "clave-vieja"))
            self.assertEqual(agents.agent_users(), [])
        self.assertEqual(User.objects.count(), 0)


class HashearClaveCommandTests(TestCase):
    """`manage.py hashear_clave`: a password's fingerprint, printed locally, so
    a password can be set straight in the database without being shared."""

    def run_command(self, *args, **kwargs):
        out = StringIO()
        call_command("hashear_clave", *args, stdout=out, **kwargs)
        return out.getvalue().strip()

    def test_it_prints_only_a_hash_that_accepts_the_password(self):
        encoded = self.run_command(password="clave-larga")
        self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
        self.assertEqual(len(encoded.splitlines()), 1)
        self.assertNotIn("clave-larga", encoded)
        self.assertTrue(check_password("clave-larga", encoded))
        self.assertFalse(check_password("otra-clave", encoded))

    def test_the_hash_set_on_an_account_logs_it_in(self):
        user = agents.create_user("samuel", "clave-vieja", "Samuel", master=True)
        user.password = self.run_command("samuel", password="clave-nueva-1")
        user.save(update_fields=["password"])
        self.assertIsNotNone(agents.authenticate("samuel", "clave-nueva-1"))
        self.assertIsNone(agents.authenticate("samuel", "clave-vieja"))

    def test_it_applies_the_password_floor(self):
        for bad, message in (("corta", "al menos 8"), ("12345678", "solo números")):
            with self.subTest(bad):
                with self.assertRaisesMessage(CommandError, message):
                    self.run_command(password=bad)

    def test_the_username_refuses_a_password_equal_to_it(self):
        with self.assertRaisesMessage(CommandError, "igual al usuario"):
            self.run_command("samuelperez", password="SamuelPerez")

    def test_it_prompts_twice_and_refuses_a_mismatch(self):
        import core.management.commands.hashear_clave as cmd

        with mock.patch.object(cmd, "getpass", side_effect=["clave-larga", "clave-larga"]):
            self.assertTrue(check_password("clave-larga", self.run_command()))
        with mock.patch.object(cmd, "getpass", side_effect=["uno-largo-1", "dos-largo-2"]):
            with self.assertRaisesMessage(CommandError, "no coinciden"):
                self.run_command()

    def test_the_old_app_agents_modes_are_gone(self):
        """Several usernames used to print a whole APP_AGENTS line."""
        with self.assertRaises(CommandError):
            self.run_command("Admin", "Samuel", password="clave-larga")


class AdminAccountsAreNotTeammatesTests(TestCase):
    """A Django staff/superuser row must never be listed or editable on the
    Usuarios page: a master could reset its password and walk into /admin."""

    def setUp(self):
        self.master = agents.create_user("jefe", "clave-larga", "Jefe", master=True)
        self.staff = User.objects.create_user("djangoadmin", password="clave-larga")
        self.staff.is_staff = True
        self.staff.save()

    def test_a_staff_row_is_not_an_agent(self):
        self.assertFalse(agents._is_app_user(self.staff))
        self.assertNotIn(self.staff, agents.agent_users())

    def test_it_is_not_listed_on_the_usuarios_page(self):
        self.client.force_login(self.master)
        html = self.client.get(
            reverse("section", args=["crm"]), {"view": "usuarios"}
        ).content.decode()
        self.assertNotIn("djangoadmin", html)

    def test_its_password_cannot_be_reset_from_here(self):
        self.client.force_login(self.master)
        response = self.client.post(
            reverse("usuario_update", args=[self.staff.pk]),
            {"display_name": "x", "password": "nueva-clave", "password2": "nueva-clave"},
        )
        self.assertContains(response, "no se gestiona aquí")
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("clave-larga"))   # untouched


class LastMasterGuardTests(TestCase):
    """The team is only administrable from the database, so the final master
    must not be able to remove themselves."""

    def test_the_only_master_cannot_be_demoted_or_deactivated(self):
        solo = agents.create_user("jefe", "clave-larga", "Jefe", master=True)
        with self.assertRaises(agents.LastMaster):
            agents.update_user(solo, "Jefe", master=False)
        with self.assertRaises(agents.LastMaster):
            agents.set_user_active(solo, False)
        solo.refresh_from_db()
        self.assertTrue(agents.is_master(solo))
        self.assertTrue(solo.is_active)

    def test_with_a_second_master_the_first_may_step_down(self):
        one = agents.create_user("uno", "clave-larga", "Uno", master=True)
        agents.create_user("dos", "clave-larga", "Dos", master=True)
        agents.update_user(one, "Uno", master=False)
        self.assertFalse(agents.is_master(one))

    def test_a_superuser_satisfies_the_guard(self):
        # A superuser can always sign in and administer the team.
        User.objects.create_superuser("root", password="clave-larga")
        solo = agents.create_user("jefe", "clave-larga", "Jefe", master=True)
        agents.update_user(solo, "Jefe", master=False)
        self.assertFalse(agents.is_master(solo))


class DeactivationEndsSessionsTests(TestCase):
    def test_deactivating_drops_the_live_session(self):
        lucia = agents.create_user("lucia", "clave-larga", "Lucía")
        self.client.force_login(lucia)
        self.assertEqual(Session.objects.count(), 1)
        agents.set_user_active(lucia, False)
        self.assertEqual(Session.objects.count(), 0)

    @override_settings(TESTING=False)
    def test_a_deactivated_user_is_stopped_on_the_next_request(self):
        lucia = agents.create_user("lucia", "clave-larga", "Lucía")
        self.client.force_login(lucia)
        session = self.client.session
        session[SESSION_KEY] = True
        session.save()
        self.assertEqual(self.client.get(reverse("section", args=["crm"])).status_code, 200)

        lucia.is_active = False
        lucia.save(update_fields=["is_active"])   # bypass end_sessions
        response = self.client.get(reverse("section", args=["crm"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    @override_settings(TESTING=False)
    def test_the_public_legal_pages_stay_reachable_without_a_session(self):
        # Meta's app review reads them without an account.
        for name in ("privacy", "data_deletion"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class SelfPasswordResetTests(TestCase):
    def test_resetting_your_own_password_does_not_log_you_out(self):
        jefe = agents.create_user("jefe", "clave-larga", "Jefe", master=True)
        self.client.force_login(jefe)
        response = self.client.post(
            reverse("usuario_update", args=[jefe.pk]),
            {"display_name": "Jefe", "master": "1",
             "password": "otra-clave-larga", "password2": "otra-clave-larga"},
        )
        self.assertContains(response, "data-dialog-dismiss")
        # Still signed in on the very next request.
        self.assertEqual(
            self.client.get(reverse("section", args=["crm"])).status_code, 200
        )
