"""Tests for agents: database logins, the gate they open, and the Inbox's
per-conversation assignment.

Every account is a database row, created in each test the way
CRM > Equipo > Usuarios or ``manage.py crear_maestro`` creates it -- nothing
about users comes from settings or the environment.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import agents
from core.middleware import SESSION_KEY
from core.models import Client
from messaging.models import Conversation


def make_team():
    """The two masters most classes start with, in display order."""
    admin = agents.create_user("Admin", "clave-de-admin", "Admin", master=True)
    samuel = agents.create_user("Samuel", "clave-de-samuel", "Samuel", master=True)
    return admin, samuel


class AuthenticateTests(TestCase):
    def setUp(self):
        self.admin, self.samuel = make_team()

    def test_the_right_password_returns_that_user(self):
        self.assertEqual(agents.authenticate("Samuel", "clave-de-samuel"), self.samuel)
        self.assertEqual(agents.authenticate("Admin", "clave-de-admin"), self.admin)

    def test_a_wrong_password_or_unknown_user_returns_nothing(self):
        self.assertIsNone(agents.authenticate("Samuel", "clave-de-samuel "))
        self.assertIsNone(agents.authenticate("Nadie", "clave-de-samuel"))
        self.assertIsNone(agents.authenticate("", ""))

    def test_credentials_do_not_cross_between_users(self):
        """Samuel's password must not open Admin's account, or vice versa."""
        self.assertIsNone(agents.authenticate("Admin", "clave-de-samuel"))
        self.assertIsNone(agents.authenticate("Samuel", "clave-de-admin"))

    def test_the_stored_hash_itself_is_not_a_password(self):
        """Someone who reads the database and pastes what they found gets nowhere."""
        self.assertIsNone(agents.authenticate("Samuel", self.samuel.password))

    def test_a_deactivated_user_is_refused(self):
        agents.set_user_active(self.admin, False)   # Samuel remains a master
        self.assertIsNone(agents.authenticate("Admin", "clave-de-admin"))

    def test_a_row_without_a_usable_password_is_refused(self):
        ghost = get_user_model().objects.create(username="fantasma")
        ghost.set_unusable_password()
        ghost.save()
        self.assertIsNone(agents.authenticate("fantasma", "cualquier-clave"))

    def test_a_miss_still_pays_for_one_hash(self):
        """A hit and a miss must cost the same, or timing tells which
        usernames exist."""
        with mock.patch("core.agents.make_password", wraps=make_password) as dummy:
            agents.authenticate("Nadie", "clave-de-samuel")
        dummy.assert_called_once()


class NoEnvironmentLoginsTests(TestCase):
    """APP_AGENTS and APP_LOGIN_* once seeded accounts. They are retired:
    even if settings still carried them, nothing reads them."""

    ENV = {
        "APP_AGENTS": "Admin:admin-pw:Admin",
        "APP_LOGIN_USERNAME": "viejo",
        "APP_LOGIN_PASSWORD": "clave-vieja",
    }

    def test_listing_agents_creates_nobody(self):
        with override_settings(**self.ENV):
            self.assertEqual(agents.agent_users(), [])
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_the_old_seed_opens_nothing(self):
        with override_settings(**self.ENV):
            self.assertIsNone(agents.authenticate("Admin", "admin-pw"))
            self.assertIsNone(agents.authenticate("viejo", "clave-vieja"))

    def test_an_existing_account_keeps_its_own_password(self):
        admin, _ = make_team()
        with override_settings(**self.ENV):
            self.assertIsNone(agents.authenticate("Admin", "admin-pw"))
            self.assertEqual(agents.authenticate("Admin", "clave-de-admin"), admin)

    def test_the_seed_code_is_gone(self):
        for name in ("configured_agents", "import_env_agents", "Agent"):
            with self.subTest(name):
                self.assertFalse(hasattr(agents, name))


class AgentUsersTests(TestCase):
    def test_everyone_active_with_a_password_is_listed_by_name(self):
        admin, samuel = make_team()
        lucia = agents.create_user("lucia", "clave-larga", "Lucía")
        self.assertEqual(agents.agent_users(), [admin, lucia, samuel])

    def test_nobody_yields_no_options(self):
        self.assertEqual(agents.agent_users(), [])


@override_settings(TESTING=False)
class AgentLoginTests(TestCase):
    """The gate starts a real auth session, so an agent is an identity."""

    def setUp(self):
        make_team()

    def test_each_agent_can_log_in_with_their_own_password(self):
        for username, password in (("Admin", "clave-de-admin"), ("Samuel", "clave-de-samuel")):
            with self.subTest(agent=username):
                self.client.post(
                    reverse("login"), {"username": username, "password": password}
                )
                self.assertTrue(self.client.session.get(SESSION_KEY))
                self.assertEqual(
                    get_user_model().objects.get(pk=self.client.session["_auth_user_id"]).username,
                    username,
                )
                self.client.get(reverse("logout"))

    def test_login_makes_request_user_that_agent(self):
        self.client.post(reverse("login"), {"username": "Samuel", "password": "clave-de-samuel"})
        response = self.client.get(reverse("section", args=["inbox"]))
        self.assertEqual(response.wsgi_request.user.username, "Samuel")

    def test_wrong_password_starts_no_auth_session(self):
        response = self.client.post(
            reverse("login"), {"username": "Samuel", "password": "nope"}
        )
        self.assertContains(response, "incorrectos")
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_clears_the_auth_session_too(self):
        self.client.post(reverse("login"), {"username": "Samuel", "password": "clave-de-samuel"})
        self.client.get(reverse("logout"))
        self.assertNotIn("_auth_user_id", self.client.session)


class ConversationAssignmentTests(TestCase):
    def setUp(self):
        self.contact = Client.objects.create(
            first_name="Ana", last_name="Ruiz", phone="+573000000001", channel="whatsapp"
        )
        self.conversation = Conversation.objects.create(
            contact=self.contact,
            channel="whatsapp",
            # Inside the 24h window, so the composer (and its Respuestas
            # rápidas button) renders instead of the closed-window notice.
            last_inbound_at=timezone.now(),
        )
        self.admin, self.samuel = make_team()

    def url(self):
        return reverse("inbox_assign", args=[self.conversation.pk])

    def test_posting_an_agent_id_assigns_the_conversation(self):
        response = self.client.post(self.url(), {"agent": str(self.samuel.pk)})
        self.assertEqual(response.status_code, 200)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.assigned_to, self.samuel)

    def test_posting_a_blank_agent_unassigns(self):
        self.conversation.assigned_to = self.samuel
        self.conversation.save(update_fields=["assigned_to"])

        self.client.post(self.url(), {"agent": ""})
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.assigned_to)

    def test_reassigning_replaces_rather_than_adds(self):
        self.client.post(self.url(), {"agent": str(self.samuel.pk)})
        self.client.post(self.url(), {"agent": str(self.admin.pk)})
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.assigned_to, self.admin)

    def test_a_user_who_is_not_an_agent_is_rejected(self):
        """The dropdown is a fixed list, so anything else is a crafted POST.
        A row with no usable password (a script's, /admin's) is not an agent --
        users created from the Usuarios page have one and ARE."""
        outsider = get_user_model().objects.create_user("intruso")
        outsider.set_unusable_password()
        outsider.save()
        response = self.client.post(self.url(), {"agent": str(outsider.pk)})
        self.assertEqual(response.status_code, 400)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.assigned_to)

    def test_a_nonsense_agent_id_is_rejected(self):
        response = self.client.post(self.url(), {"agent": "no-soy-un-id"})
        self.assertEqual(response.status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_unknown_conversation_404s(self):
        response = self.client.post(reverse("inbox_assign", args=[9999]), {"agent": ""})
        self.assertEqual(response.status_code, 404)

    def test_response_carries_the_new_name_for_both_panels(self):
        """The control comes back as the swap target; the details panel's
        "Asignada a" line rides along out-of-band."""
        response = self.client.post(self.url(), {"agent": str(self.samuel.pk)})
        html = response.content.decode()
        self.assertIn(f'id="chat-assign-{self.conversation.pk}"', html)
        self.assertIn(f'id="details-assigned-{self.conversation.pk}"', html)
        self.assertIn('hx-swap-oob="outerHTML"', html)
        self.assertIn("Samuel", html)

    def test_chat_panel_renders_the_dropdown_with_every_agent(self):
        self.conversation.assigned_to = self.samuel
        self.conversation.save(update_fields=["assigned_to"])

        response = self.client.get(reverse("inbox_chat", args=[self.conversation.pk]))
        html = response.content.decode()
        self.assertIn("Sin asignar", html)
        self.assertIn(f'value="{self.admin.pk}"', html)
        self.assertInHTML(
            f'<option value="{self.samuel.pk}" selected>Samuel</option>', html
        )

    def test_a_deactivated_assignee_still_shows_as_assigned(self):
        """A <select> with no matching option silently shows its first entry --
        which would claim an assigned chat is "Sin asignar"."""
        self.conversation.assigned_to = self.samuel
        self.conversation.save(update_fields=["assigned_to"])

        agents.set_user_active(self.samuel, False)
        response = self.client.get(reverse("inbox_chat", args=[self.conversation.pk]))
        self.assertInHTML(
            f'<option value="{self.samuel.pk}" selected>Samuel</option>',
            response.content.decode(),
        )

    def test_reassigning_away_drops_a_non_agent_from_the_options(self):
        """The old assignee is only listed to keep them visible while they
        hold the chat -- once they don't, they shouldn't linger in the list."""
        outsider = get_user_model().objects.create_user("asesor")
        outsider.set_unusable_password()   # not an agent: no login of its own
        outsider.first_name = "Asesor Demo"
        outsider.save()
        self.conversation.assigned_to = outsider
        self.conversation.save(update_fields=["assigned_to"])

        response = self.client.post(self.url(), {"agent": str(self.samuel.pk)})
        self.assertNotIn("Asesor Demo", response.content.decode())

    def test_composer_offers_quick_replies(self):
        """The picker is wired to its endpoint (tests_respuestas_rapidas has
        the behavior; this pins that the composer carries it)."""
        response = self.client.get(reverse("inbox_chat", args=[self.conversation.pk]))
        html = response.content.decode()
        self.assertIn("Respuestas rápidas", html)
        self.assertIn(reverse("inbox_quick_replies", args=[self.conversation.pk]), html)


class TuInboxFilterTests(TestCase):
    """"Tu inbox" was dead while nobody had an identity; agents give it one."""

    def setUp(self):
        contact = Client.objects.create(
            first_name="Ana", phone="+573000000001", channel="whatsapp"
        )
        self.admin, self.samuel = make_team()
        self.mine = Conversation.objects.create(
            contact=contact, channel="whatsapp", assigned_to=self.samuel
        )
        self.theirs = Conversation.objects.create(
            contact=contact, channel="whatsapp", assigned_to=self.admin
        )
        self.nobodys = Conversation.objects.create(contact=contact, channel="whatsapp")

    def test_tu_inbox_shows_only_the_logged_in_agents_conversations(self):
        self.client.force_login(self.samuel)
        response = self.client.get(reverse("inbox_list", args=["tu-inbox"]))
        html = response.content.decode()
        self.assertIn(f"/inbox/chat/{self.mine.pk}/", html)
        self.assertNotIn(f"/inbox/chat/{self.theirs.pk}/", html)

    def test_sin_asignar_shows_the_unassigned_one(self):
        response = self.client.get(reverse("inbox_list", args=["sin-asignar"]))
        html = response.content.decode()
        self.assertIn(f"/inbox/chat/{self.nobodys.pk}/", html)
        self.assertNotIn(f"/inbox/chat/{self.mine.pk}/", html)
