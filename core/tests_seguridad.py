"""Tests for the login gate's harder edges: hashed APP_AGENTS seeds, the
non-ASCII crash, and the doors a Usuarios master must not be able to open."""

from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core import agents
from core.checks import plaintext_env_secrets
from core.middleware import SESSION_KEY

User = get_user_model()
HASH = make_password("clave-larga")


class NonAsciiLoginTests(TestCase):
    """`hmac.compare_digest` on str raises TypeError for any non-ASCII
    character, so a login as "José" used to 500 before checking anything."""

    @override_settings(APP_AGENTS="Admin:admin-pw:Admin")
    def test_a_non_ascii_username_is_rejected_not_a_crash(self):
        self.assertIsNone(agents.authenticate("José", "admin-pw"))
        self.assertIsNone(agents.authenticate("Ünïcödé", "x"))

    @override_settings(APP_AGENTS="José:clave:José")
    def test_a_non_ascii_username_can_still_log_in(self):
        self.assertIsNotNone(agents.authenticate("José", "clave"))
        self.assertIsNone(agents.authenticate("José", "otra"))


class HashedEnvSecretTests(TestCase):
    @override_settings(APP_AGENTS=f"Admin:{HASH}:Admin")
    def test_a_hashed_secret_authenticates(self):
        self.assertIsNotNone(agents.authenticate("Admin", "clave-larga"))
        self.assertIsNone(agents.authenticate("Admin", "otra-clave"))

    @override_settings(APP_AGENTS=f"Admin:{HASH}:Admin")
    def test_the_hash_itself_is_not_a_password(self):
        self.assertIsNone(agents.authenticate("Admin", HASH))

    @override_settings(APP_AGENTS="Admin:admin-pw:Admin")
    def test_a_raw_secret_still_works_so_a_redeploy_locks_nobody_out(self):
        self.assertIsNotNone(agents.authenticate("Admin", "admin-pw"))

    @override_settings(APP_AGENTS=f"Uno:{HASH}:Uno,Dos:raw-pw:Dos")
    def test_hashed_and_raw_entries_coexist(self):
        self.assertIsNotNone(agents.authenticate("Uno", "clave-larga"))
        self.assertIsNotNone(agents.authenticate("Dos", "raw-pw"))
        self.assertIsNone(agents.authenticate("Uno", "raw-pw"))

    def test_is_hashed_does_not_mistake_a_long_passphrase_for_md5(self):
        # identify_hasher reads any bare 32-char string as unsalted MD5.
        self.assertFalse(agents.Agent("u", "x" * 32, "u").is_hashed)
        self.assertFalse(agents.Agent("u", "no-dollar-sign", "u").is_hashed)
        self.assertFalse(agents.Agent("u", "nosuchalgo$rest", "u").is_hashed)
        self.assertTrue(agents.Agent("u", HASH, "u").is_hashed)


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
        # No production domain exists locally, and Meta could not fetch a local
        # link anyway; a warning here would only train people to ignore it.
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


class PlaintextWarningTests(TestCase):
    @override_settings(APP_AGENTS="Admin:admin-pw:Admin,Otro:otra:Otro")
    def test_it_names_every_plaintext_agent(self):
        [warning] = plaintext_env_secrets(None)
        self.assertEqual(warning.id, "core.W001")
        self.assertIn("'Admin'", warning.msg)
        self.assertIn("'Otro'", warning.msg)
        self.assertIn("hashear_clave", warning.hint)

    @override_settings(APP_AGENTS=f"Admin:{HASH}:Admin")
    def test_a_fully_hashed_environment_is_quiet(self):
        self.assertEqual(plaintext_env_secrets(None), [])


class HashearClaveCommandTests(TestCase):
    def test_it_prints_a_pasteable_entry(self):
        out = StringIO()
        call_command("hashear_clave", "Samuel", password="clave-larga", stdout=out)
        printed = out.getvalue()
        self.assertIn("Samuel:", printed)
        self.assertIn("pbkdf2_sha256$", printed)
        # And the hash it printed actually accepts the password.
        entry = next(l for l in printed.splitlines() if l.startswith("Samuel:"))
        with override_settings(APP_AGENTS=entry.strip()):
            self.assertIsNotNone(agents.authenticate("Samuel", "clave-larga"))

    def test_it_refuses_a_password_below_the_floor(self):
        with self.assertRaises(Exception):
            call_command("hashear_clave", "Samuel", password="corta")


@override_settings(APP_AGENTS="Admin:admin-pw:Admin")
class AdminAccountsAreNotTeammatesTests(TestCase):
    """A Django staff/superuser row must never be listed or editable on the
    Usuarios page: a master could reset its password and walk into /admin."""

    def setUp(self):
        self.staff = User.objects.create_user("djangoadmin", password="clave-larga")
        self.staff.is_staff = True
        self.staff.save()

    def test_a_staff_row_is_not_an_agent(self):
        self.assertFalse(agents._is_app_user(self.staff))
        self.assertNotIn(self.staff, agents.agent_users())

    def test_it_is_not_listed_on_the_usuarios_page(self):
        self.client.force_login(agents.authenticate("Admin", "admin-pw"))
        html = self.client.get(
            reverse("section", args=["crm"]), {"view": "usuarios"}
        ).content.decode()
        self.assertNotIn("djangoadmin", html)

    def test_its_password_cannot_be_reset_from_here(self):
        self.client.force_login(agents.authenticate("Admin", "admin-pw"))
        response = self.client.post(
            reverse("usuario_update", args=[self.staff.pk]),
            {"display_name": "x", "password": "nueva-clave", "password2": "nueva-clave"},
        )
        self.assertContains(response, "no se gestiona aquí")
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("clave-larga"))   # untouched


class LastMasterGuardTests(TestCase):
    """With APP_AGENTS empty, the team is only administrable from the
    database -- so the final master must not be able to remove themselves."""

    @override_settings(APP_AGENTS="", APP_LOGIN_USERNAME="", APP_LOGIN_PASSWORD="")
    def test_the_only_master_cannot_be_demoted_or_deactivated(self):
        solo = agents.create_user("jefe", "clave-larga", "Jefe", master=True)
        with self.assertRaises(agents.LastMaster):
            agents.update_user(solo, "Jefe", master=False)
        with self.assertRaises(agents.LastMaster):
            agents.set_user_active(solo, False)
        solo.refresh_from_db()
        self.assertTrue(agents.is_master(solo))
        self.assertTrue(solo.is_active)

    @override_settings(APP_AGENTS="", APP_LOGIN_USERNAME="", APP_LOGIN_PASSWORD="")
    def test_with_a_second_master_the_first_may_step_down(self):
        one = agents.create_user("uno", "clave-larga", "Uno", master=True)
        agents.create_user("dos", "clave-larga", "Dos", master=True)
        agents.update_user(one, "Uno", master=False)
        self.assertFalse(agents.is_master(one))

    @override_settings(APP_AGENTS="Admin:admin-pw:Admin")
    def test_seeded_agents_satisfy_the_guard(self):
        # The seed is imported as a master with a real password before the
        # count, so it names someone who can administer the team.
        solo = agents.create_user("jefe", "clave-larga", "Jefe", master=True)
        agents.update_user(solo, "Jefe", master=False)
        self.assertFalse(agents.is_master(solo))


@override_settings(APP_AGENTS="Admin:admin-pw:Admin")
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


@override_settings(APP_AGENTS="Admin:admin-pw:Admin")
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


class HashearClaveManyAgentsTests(TestCase):
    """Rehashing a team by hand is where a stray comma produces an
    APP_AGENTS that parses to fewer agents than you meant -- on a deploy
    nobody can then log in to fix."""

    def test_several_agents_print_one_pasteable_line(self):
        # The command prompts per agent; drive it with a scripted getpass.
        import core.management.commands.hashear_clave as cmd
        answers = iter(["clave-larga-1", "clave-larga-1", "clave-larga-2", "clave-larga-2"])
        with mock.patch.object(cmd, "getpass", lambda *a, **k: next(answers)):
            out = StringIO()
            call_command("hashear_clave", "Admin", "Samuel", stdout=out)
        line = next(l for l in out.getvalue().splitlines() if l.startswith("APP_AGENTS="))
        with override_settings(APP_AGENTS=line.split("=", 1)[1]):
            self.assertEqual(
                [a.username for a in agents.configured_agents()], ["Admin", "Samuel"]
            )
            self.assertIsNotNone(agents.authenticate("Admin", "clave-larga-1"))
            self.assertIsNotNone(agents.authenticate("Samuel", "clave-larga-2"))
            self.assertIsNone(agents.authenticate("Admin", "clave-larga-2"))
