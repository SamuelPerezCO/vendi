"""Tests for the Configuración de mensajería screen: headless flat nav, the
Plantillas de WhatsApp tabbed table, the two-half empty state, and the
template chooser modal both create buttons open."""

import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.mensajeria import TABLE_COLUMNS, TABS, VIEWS
from core.models import MessageTemplate

HTMX = {"HX-Request": "true"}


def make_template(name: str, status: str = "pendiente", active: bool = True):
    """One template row; tests create their own data, there are no seeds."""
    return MessageTemplate.objects.create(
        name=name, sub_type="custom", category="marketing",
        body=f"Hola, {name}", team="Ventas", status=status, is_active=active,
    )


class MensajeriaScreenTests(TestCase):
    def test_renders_both_columns(self):
        response = self.client.get(reverse("section", args=["mensajeria"]))
        self.assertContains(response, "side-nav")
        self.assertContains(response, 'id="mensajeria-panel"')

    def test_nav_has_no_heading(self):
        # Unlike the other panels, this list starts at the top of the column.
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertNotIn("side-nav__heading", html)
        self.assertIn("side-nav__scroll--headless", html)

    def test_every_nav_row_renders(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertEqual(len(VIEWS), 7)
        for view in VIEWS:
            with self.subTest(view.key):
                self.assertIn(f"?view={view.key}", html)
                self.assertIn(view.label, html)

    def test_plantillas_is_the_default_view(self):
        response = self.client.get(reverse("section", args=["mensajeria"]))
        self.assertEqual(response.context["active_view"], "plantillas-whatsapp")
        self.assertEqual(
            response.context["panel_template"],
            "partials/mensajeria/panels/plantillas-whatsapp.html",
        )

    def test_view_query_param_selects_the_panel(self):
        # Respuestas rápidas, then Widget de WhatsApp, were the placeholder
        # example here; both are real pages now. Reglas de mensajería is the
        # stand-in until it is built too.
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"view": "reglas-mensajeria"}
        )
        self.assertEqual(response.context["active_view"], "reglas-mensajeria")
        self.assertContains(response, "Reglas de mensajería — próximamente")

        real = self.client.get(
            reverse("section", args=["mensajeria"]), {"view": "respuestas-rapidas"}
        )
        self.assertContains(real, "+ Crear respuesta")

    def test_unknown_view_falls_back_instead_of_404(self):
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"view": "bogus"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_view"], "plantillas-whatsapp")

    def test_exactly_one_row_is_active(self):
        html = self.client.get(
            reverse("section", args=["mensajeria"]), {"view": "reglas-mensajeria"}
        ).content.decode()
        self.assertEqual(html.count("side-nav__row is-active"), 1)
        active = html.split('?view=reglas-mensajeria"')[0].rsplit("<a ", 1)[-1]
        self.assertIn("is-active", active)

    def test_section_returns_a_bare_fragment_to_htmx(self):
        body = self.client.get(
            reverse("section", args=["mensajeria"]), headers=HTMX
        ).content.decode()
        self.assertNotIn("<html", body)
        self.assertNotIn("sidebar", body)
        self.assertIn("mensajeria-panel", body)


class PlantillasPanelTests(TestCase):
    def test_toolbar_renders_without_a_page_title(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertIn("Buscar por nombre", html)
        self.assertIn("Crear plantilla +", html)
        # No heading in the panel, per the reference: the toolbar sits alone.
        panel = html.split('id="mensajeria-panel"', 1)[1]
        self.assertNotIn("<h1", panel.split('id="template-table"')[0])

    def test_all_columns_render(self):
        response = self.client.get(reverse("section", args=["mensajeria"]))
        for column in TABLE_COLUMNS:
            self.assertContains(response, column)

    def test_no_checkbox_column(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertNotIn('type="checkbox"', html)

    def test_empty_state_renders_both_halves(self):
        response = self.client.get(reverse("section", args=["mensajeria"]))
        self.assertContains(
            response, "Crea plantillas de WhatsApp listas para automatizar tus mensajes"
        )
        self.assertContains(response, "Usa plantillas para iniciar conversaciones")
        self.assertContains(response, "Crear nueva plantilla")
        self.assertContains(response, "tpl-empty__art")  # the shared illustration slot

    def test_both_create_buttons_open_the_chooser(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        # Toolbar + empty state, both triggering the one dialog the panel hosts.
        self.assertEqual(html.count('data-dialog-open="template-chooser-dialog"'), 2)
        self.assertEqual(html.count('id="template-chooser-dialog"'), 1)

    def test_empty_state_hidden_once_templates_exist(self):
        make_template("Bienvenida")
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertIn("Bienvenida", html)
        self.assertNotIn("tpl-empty", html)


class PlantillasTabTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_template("Pendiente A", status="pendiente")
        make_template("Aceptada B", status="aceptada")
        make_template("Rechazada C", status="rechazada")
        make_template("Apagada D", status="aceptada", active=False)

    def test_todas_is_the_default_tab_and_shows_everything(self):
        response = self.client.get(reverse("section", args=["mensajeria"]))
        self.assertEqual(response.context["active_tab"], "todas")
        for name in ("Pendiente A", "Aceptada B", "Rechazada C", "Apagada D"):
            self.assertContains(response, name)

    def test_status_tabs_filter_by_status(self):
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"tab": "aceptadas"}
        )
        self.assertContains(response, "Aceptada B")
        self.assertContains(response, "Apagada D")  # aceptada, though switched off
        self.assertNotContains(response, "Pendiente A")
        self.assertNotContains(response, "Rechazada C")

    def test_desactivadas_filters_the_toggle_not_the_status(self):
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"tab": "desactivadas"}
        )
        self.assertContains(response, "Apagada D")
        self.assertNotContains(response, "Aceptada B")

    def test_unknown_tab_falls_back_instead_of_404(self):
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"tab": "bogus"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "todas")

    def test_exactly_one_tab_is_active(self):
        html = self.client.get(
            reverse("section", args=["mensajeria"]), {"tab": "rechazadas"}
        ).content.decode()
        self.assertEqual(html.count("product-tabs__tab is-active"), 1)
        active = html.split("&tab=rechazadas")[0].rsplit("<a ", 1)[-1]
        self.assertIn("is-active", active)

    def test_row_renders_every_column_value(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        row = html.split("Aceptada B", 1)[1].split("</tr>", 1)[0]
        self.assertIn("Mensaje personalizado", row)  # tipo (sub_type display)
        self.assertIn("Marketing", row)     # categoría (display name)
        self.assertIn("Hola, Aceptada B", row)
        self.assertIn("Ventas", row)        # equipo
        self.assertIn("<td>Sí</td>", row)   # activo
        self.assertIn("Aceptada</td>", row)  # estado display

    def test_empty_tab_shows_empty_state_while_others_have_rows(self):
        MessageTemplate.objects.filter(status="rechazada").delete()
        response = self.client.get(
            reverse("section", args=["mensajeria"]), {"tab": "rechazadas"}
        )
        self.assertContains(response, "tpl-empty")


class PlantillaEditorAuthenticationTests(TestCase):
    """The Autenticación category, which is a different form: WhatsApp writes
    and localizes an OTP message itself and refuses one that arrives with its
    own body, so the editor collects three settings and no copy at all."""

    def payload(self, **overrides):
        payload = {
            "name": "codigo_verificacion",
            "category": "authentication",
            "sub_type": "auth_code",
            "language": "es",
            "team": "",
            "auth_security_recommendation": "1",
            "auth_expiration": "10",
            "auth_button_text": "Copiar código",
        }
        payload.update(overrides)
        return payload

    def test_the_editor_renders_the_panel_and_its_copy(self):
        html = self.client.get(reverse("plantilla_editor")).content.decode()
        self.assertIn("data-auth-panel", html)
        self.assertIn("Código de autenticación", html)
        self.assertIn("El código vence en (minutos)", html)
        self.assertIn("Texto del botón de copiado", html)
        # The preview needs WhatsApp's own wording to show the real message.
        self.assertIn('id="tpl-auth-preview"', html)

    def test_a_valid_post_saves_the_three_settings(self):
        self.client.post(reverse("plantilla_editor"), self.payload())
        saved = MessageTemplate.objects.get()
        self.assertEqual(saved.category, "authentication")
        self.assertEqual(saved.sub_type, "auth_code")
        self.assertTrue(saved.auth_security_recommendation)
        self.assertEqual(saved.auth_code_expiration_minutes, 10)
        self.assertEqual(saved.auth_button_text, "Copiar código")
        self.assertEqual(saved.status, "pendiente")

    def test_the_body_is_whatsapps_wording_not_anything_typed(self):
        """Stored so the Inbox has something true to show, never submitted --
        and never what the person typed into a field the panel hides."""
        self.client.post(
            reverse("plantilla_editor"),
            self.payload(body="Un cuerpo que nadie pudo escribir", language="en"),
        )
        saved = MessageTemplate.objects.get()
        self.assertEqual(saved.body, "Your verification code is {{1}}.")
        self.assertEqual(saved.body_sample_values, ["123456"])

    def test_the_copy_fields_are_not_required_and_not_kept(self):
        """The old validator asked an Autenticación plantilla for a body and
        refused it for not having one -- a field the panel never showed."""
        self.client.post(reverse("plantilla_editor"), self.payload())
        saved = MessageTemplate.objects.get()
        self.assertEqual(saved.header_type, "none")
        self.assertEqual(saved.footer, "")
        self.assertEqual(saved.buttons, [])

    def test_an_unchecked_security_recommendation_is_saved_as_off(self):
        payload = self.payload()
        del payload["auth_security_recommendation"]
        self.client.post(reverse("plantilla_editor"), payload)
        self.assertFalse(MessageTemplate.objects.get().auth_security_recommendation)

    def test_a_blank_button_label_falls_back_to_the_default(self):
        self.client.post(
            reverse("plantilla_editor"), self.payload(auth_button_text="")
        )
        self.assertEqual(MessageTemplate.objects.get().auth_button_text, "Copiar código")

    def test_the_expiry_must_be_a_number_in_range(self):
        for value in ("", "diez", "0", "91"):
            with self.subTest(value):
                response = self.client.post(
                    reverse("plantilla_editor"), self.payload(auth_expiration=value)
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_the_button_label_is_capped(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.payload(auth_button_text="x" * 26)
        )
        self.assertContains(response, "Máximo 25 caracteres")
        self.assertEqual(MessageTemplate.objects.count(), 0)


class PlantillasTableEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_template("Bienvenida")

    def test_returns_only_the_table_region(self):
        response = self.client.get(reverse("plantillas_table", args=["todas"]))
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<html", body)
        self.assertNotIn("side-nav", body)          # nav panel is not re-sent
        self.assertNotIn("plantillas__toolbar", body)  # toolbar stays put
        self.assertIn("product-tabs", body)         # tabs travel with the region
        self.assertIn("Bienvenida", body)

    def test_every_tab_has_a_working_endpoint(self):
        for tab in TABS:
            with self.subTest(tab.key):
                response = self.client.get(reverse("plantillas_table", args=[tab.key]))
                self.assertEqual(response.status_code, 200)

    def test_unknown_tab_is_404(self):
        self.assertEqual(
            self.client.get("/mensajeria/plantillas/tab/bogus/").status_code, 404
        )

    def test_tab_slugs_do_not_swallow_the_chooser_routes(self):
        self.assertEqual(
            self.client.get("/mensajeria/plantillas/galeria/").status_code, 200
        )
        self.assertEqual(
            self.client.get("/mensajeria/plantillas/editor/").status_code, 200
        )

    def test_tabs_target_the_table_region(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertIn('hx-target="#template-table"', html)
        self.assertIn('hx-get="/mensajeria/plantillas/tab/aceptadas/"', html)
        self.assertIn(
            'hx-push-url="/s/mensajeria/?view=plantillas-whatsapp&tab=aceptadas"', html
        )

    def test_endpoint_matches_what_the_page_embeds(self):
        fragment = self.client.get(
            reverse("plantillas_table", args=["todas"])
        ).content.decode()
        page = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertIn(fragment.strip(), page)


class TemplateChooserTests(TestCase):
    """The '¿Cómo quieres crear tu plantilla?' modal and its two destinations."""

    def get_page(self):
        return self.client.get(reverse("section", args=["mensajeria"])).content.decode()

    def test_dialog_carries_the_copy_and_a11y_wiring(self):
        html = self.get_page()
        self.assertIn("¿Cómo quieres crear tu plantilla de WhatsApp?", html)
        self.assertIn("Selecciona cómo deseas comenzar", html)
        self.assertIn('aria-labelledby="template-chooser-title"', html)
        self.assertIn("data-dialog-backdrop-close", html)
        self.assertIn('aria-label="Cerrar"', html)

    def test_cards_link_their_destinations_with_href_fallbacks(self):
        html = self.get_page()
        for name in ("plantilla_gallery", "plantilla_editor"):
            with self.subTest(name):
                url = reverse(name)
                self.assertEqual(html.count(f'hx-get="{url}"'), 1)
                self.assertEqual(html.count(f'href="{url}"'), 1)
        # Both cards swap the panel and close the dialog once that succeeds.
        self.assertEqual(html.count("data-close-on-success"), 2)

    def test_card_copy_keeps_the_button_hierarchy(self):
        html = self.get_page()
        self.assertIn("Elige la plantilla que mejor se adapte a tus objetivos.", html)
        self.assertIn("Diseña tu plantilla desde cero", html)
        # Solid button on the recommended path, outlined on the from-scratch one.
        recommended = html.split("Crear desde plantilla", 1)[1].split("</a>", 1)[0]
        self.assertIn("btn-primary", recommended)
        scratch = html.split("Crear desde cero", 1)[1].split("</a>", 1)[0]
        self.assertIn("btn-outline", scratch)

    def test_gallery_route_returns_its_placeholder(self):
        response = self.client.get(reverse("plantilla_gallery"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Galería de plantillas — próximamente")

    def test_editor_route_returns_the_editor(self):
        response = self.client.get(reverse("plantilla_editor"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Crear plantilla")
        self.assertContains(response, "Nombre de la plantilla")

    def test_dialog_stays_out_of_the_swapped_table_region(self):
        fragment = self.client.get(
            reverse("plantillas_table", args=["todas"])
        ).content.decode()
        self.assertNotIn('id="template-chooser-dialog"', fragment)
        # The empty state's trigger still works: the dialog lives in the panel.
        self.assertIn('data-dialog-open="template-chooser-dialog"', fragment)


# Header uploads land in MEDIA_ROOT; keep test files out of the real one.
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="mvp-crm-test-media-"))
class PlantillaEditorTests(TestCase):
    """The Crear plantilla editor: rendering, validation and the happy path."""

    def valid_payload(self, **overrides):
        payload = {
            "name": "bienvenida_1",
            "category": "marketing",
            "sub_type": "custom",
            "language": "es",
            "team": "",
            "header_type": "none",
            "header_text": "",
            "body": "Hola {{1}}, bienvenido a {{2}}.",
            "sample_1": "Ana",
            "sample_2": "Vendi",
            "footer": "Responde STOP para salir",
            "button_kind": "none",
        }
        payload.update(overrides)
        return payload

    def test_editor_renders_every_section(self):
        html = self.client.get(reverse("plantilla_editor")).content.decode()
        for fragment in (
            "Crear plantilla", "Nombre de la plantilla", "Elige la categoría",
            "Información básica", "Escoge la cabecera del mensaje",
            "Cuerpo del mensaje", "Pie de página", "Botones",
            "Vista previa", "Ejemplo de tu mensaje",
        ):
            with self.subTest(fragment):
                self.assertIn(fragment, html)

    def test_marketing_offers_three_sub_types_others_one(self):
        html = self.client.get(reverse("plantilla_editor")).content.decode()
        for label in ("Mensaje personalizado", "Oferta de tiempo limitado",
                      "Carrusel", "Código de autenticación"):
            with self.subTest(label):
                self.assertIn(label, html)
        # Only the default category's group is visible on first render.
        def group_tag(key):
            return html.split(f'data-subtype-group="{key}"', 1)[1].split(">", 1)[0]

        self.assertNotIn("hidden", group_tag("marketing"))
        self.assertIn("hidden", group_tag("utility"))
        self.assertIn("hidden", group_tag("authentication"))

    def test_valid_htmx_post_saves_pendiente_and_returns_the_list(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(), headers=HTMX
        )
        self.assertEqual(response.status_code, 200)
        template = MessageTemplate.objects.get(name="bienvenida_1")
        self.assertEqual(template.status, "pendiente")
        self.assertEqual(template.body_sample_values, ["Ana", "Vendi"])
        self.assertEqual(template.buttons, [])
        # The response is the Plantillas panel, not the editor again.
        self.assertContains(response, "plantillas__toolbar")
        self.assertContains(response, "bienvenida_1")

    def test_valid_plain_post_redirects_to_the_list(self):
        response = self.client.post(reverse("plantilla_editor"), self.valid_payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/s/mensajeria/?view=plantillas-whatsapp")
        self.assertEqual(MessageTemplate.objects.count(), 1)

    def test_a_refused_submission_survives_the_plain_redirect(self):
        """A plain POST answers with a redirect, which has nowhere to put the
        notice -- so it used to be computed and dropped, and the one case that
        most needs explaining arrived as a silent Pendiente row."""
        from messaging import services

        with patch.object(
            services,
            "submit_template",
            side_effect=services.TemplateSubmissionFailed(
                "WhatsApp no aceptó la plantilla: HTTP 400 -- Nombre duplicado"
            ),
        ):
            response = self.client.post(
                reverse("plantilla_editor"), self.valid_payload()
            )
        self.assertEqual(response.status_code, 302)

        panel = self.client.get(response.url).content.decode()
        self.assertIn("Nombre duplicado", panel)
        # Read once: it belongs on the page after the save, not on every
        # visit after that.
        self.assertNotIn("Nombre duplicado", self.client.get(response.url).content.decode())

    def test_the_htmx_path_still_shows_it_inline(self):
        from messaging import services

        with patch.object(
            services,
            "submit_template",
            side_effect=services.TemplateSubmissionFailed("HTTP 400 -- Nombre duplicado"),
        ):
            response = self.client.post(
                reverse("plantilla_editor"), self.valid_payload(), headers=HTMX
            )
        self.assertContains(response, "Nombre duplicado")

    def test_plain_get_renders_inside_the_page_shell(self):
        html = self.client.get(reverse("plantilla_editor")).content.decode()
        self.assertIn("<html", html)
        self.assertIn("side-nav", html)          # the mensajería secondary nav
        self.assertIn("Nombre de la plantilla", html)

    def test_htmx_get_returns_a_bare_fragment(self):
        body = self.client.get(
            reverse("plantilla_editor"), headers=HTMX
        ).content.decode()
        self.assertNotIn("<html", body)
        self.assertNotIn("side-nav", body)
        self.assertIn("Nombre de la plantilla", body)

    def test_name_and_team_lengths_are_capped(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(name="a" * 121)
        )
        self.assertContains(response, "Máximo 120 caracteres.")
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(team="x" * 81)
        )
        self.assertContains(response, "Máximo 80 caracteres.")
        self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_header_text_rejects_variables(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(header_type="text", header_text="Hola {{1}}"),
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "La cabecera no admite variables.")

    def test_zero_padded_variables_are_rejected(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(body="Hola {{01}}", sample_1="Ana"),
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "sin ceros a la izquierda")

    def test_cta_needs_a_real_url_and_phone_shape(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="cta", cta_url_text="Ir", cta_url="https://"
            ),
        )
        self.assertContains(response, "URL válida")
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="cta", cta_phone_text="Llámanos", cta_phone="abc"
            ),
        )
        self.assertContains(response, "código de país")
        self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_a_call_button_needs_the_country_code(self):
        """A bare local number used to pass here and be refused by WhatsApp
        at submission, where nobody could see why."""
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="cta", cta_phone_text="Llámanos", cta_phone="300 123 4567"
            ),
        )
        self.assertContains(response, "código de país")
        self.assertEqual(MessageTemplate.objects.count(), 0)

        self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="cta",
                cta_phone_text="Llámanos",
                cta_phone="+57 300 123 4567",
            ),
        )
        saved = MessageTemplate.objects.get()
        self.assertEqual(
            saved.buttons,
            [{"type": "phone", "text": "Llámanos", "phone": "+57 300 123 4567"}],
        )

    def test_a_variable_may_not_open_close_or_double_up_the_body(self):
        """The three placements WhatsApp refuses at submission. Caught here so
        the editor explains them instead of the plantilla being saved and
        bounced minutes later with nothing on screen to say why."""
        cases = {
            "{{1}}, tu pedido está listo.": "no puede empezar",
            "Hola, tu pedido es {{1}}": "no puede terminar",
            "Hola {{1}} {{2}}, pasa por él.": "no pueden ir seguidas",
        }
        for body, expected in cases.items():
            with self.subTest(body):
                response = self.client.post(
                    reverse("plantilla_editor"),
                    self.valid_payload(body=body, sample_1="Ana", sample_2="#4512"),
                )
                self.assertContains(response, expected)
        self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_a_body_with_variables_in_the_middle_is_fine(self):
        """The guard above must not refuse the ordinary case it exists to
        protect -- variables with words on both sides and between them."""
        self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(body="Hola {{1}}, tu pedido {{2}} está listo."),
        )
        self.assertEqual(MessageTemplate.objects.count(), 1)

    def test_sub_types_without_an_editor_are_refused_not_downgraded(self):
        """Carrusel and Oferta de tiempo limitado have nowhere to enter their
        cards or their offer. Submitted anyway they reach WhatsApp as plain
        custom templates and come back approved as something else, so the
        editor says no until the authoring UI exists."""
        for sub_type in ("carousel", "limited_time_offer"):
            with self.subTest(sub_type):
                response = self.client.post(
                    reverse("plantilla_editor"),
                    self.valid_payload(sub_type=sub_type),
                )
                self.assertContains(response, "aún no se puede crear")
        self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_media_header_rejects_a_mismatched_file_type(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("nota.txt", b"hola", content_type="text/plain")
        response = self.client.post(
            reverse("plantilla_editor"),
            {**self.valid_payload(header_type="image"), "header_media": bad},
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "no coincide con el tipo de cabecera")

        good = SimpleUploadedFile("foto.png", b"\x89PNG", content_type="image/png")
        self.client.post(
            reverse("plantilla_editor"),
            {**self.valid_payload(header_type="image"), "header_media": good},
        )
        self.assertEqual(MessageTemplate.objects.count(), 1)

    def test_samples_store_in_numeric_order_even_if_body_reverses_them(self):
        self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                body="Pedido {{2}} para {{1}}.", sample_1="Ana", sample_2="A-9"
            ),
        )
        template = MessageTemplate.objects.get(name="bienvenida_1")
        # Element i is the sample for {{i+1}}, whatever the reading order.
        self.assertEqual(template.body_sample_values, ["Ana", "A-9"])

    def test_name_must_match_the_meta_regex(self):
        for bad_name in ("Bienvenida", "hola mundo", "hola-mundo", "ñandu", ""):
            with self.subTest(bad_name):
                response = self.client.post(
                    reverse("plantilla_editor"), self.valid_payload(name=bad_name)
                )
                self.assertEqual(MessageTemplate.objects.count(), 0)
                self.assertContains(response, "tpl-editor__error")

    def test_name_is_unique_per_language(self):
        self.client.post(reverse("plantilla_editor"), self.valid_payload())
        response = self.client.post(reverse("plantilla_editor"), self.valid_payload())
        self.assertEqual(MessageTemplate.objects.count(), 1)
        self.assertContains(response, "Ya existe una plantilla")
        # Same name in another language is fine.
        self.client.post(
            reverse("plantilla_editor"), self.valid_payload(language="en_US")
        )
        self.assertEqual(MessageTemplate.objects.count(), 2)

    def test_body_is_required_and_capped(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(body="")
        )
        self.assertContains(response, "El cuerpo del mensaje es obligatorio.")
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(body="x" * 1025)
        )
        self.assertContains(response, "Máximo 1024 caracteres.")
        self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_every_variable_needs_a_sample_value(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(sample_2=""),
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(
            response, "Escribe un valor de ejemplo para cada variable."
        )

    def test_variables_must_be_sequential_from_one(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(body="Hola {{1}} y {{3}}", sample_1="a", sample_3="b"),
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "sin saltos")

    def test_footer_rejects_variables_and_overflow(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(footer="Adiós {{1}}")
        )
        self.assertContains(response, "El pie de página no admite variables.")
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(footer="x" * 61)
        )
        self.assertContains(response, "Máximo 60 caracteres.")
        self.assertEqual(MessageTemplate.objects.count(), 0)

    def test_text_header_requires_its_text(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(header_type="text", header_text=""),
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "Escribe el texto de la cabecera.")

    def test_media_header_requires_a_file(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(header_type="image")
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "Sube un archivo de ejemplo")

    def test_quick_reply_buttons_are_saved(self):
        self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="quick", quick_reply_1="Sí", quick_reply_2="No"
            ),
        )
        template = MessageTemplate.objects.get(name="bienvenida_1")
        self.assertEqual(
            template.buttons,
            [
                {"type": "quick_reply", "text": "Sí"},
                {"type": "quick_reply", "text": "No"},
            ],
        )

    def test_quick_kind_without_any_button_errors(self):
        response = self.client.post(
            reverse("plantilla_editor"), self.valid_payload(button_kind="quick")
        )
        self.assertEqual(MessageTemplate.objects.count(), 0)
        self.assertContains(response, "al menos un botón")

    def test_cta_url_button_is_validated_and_saved(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="cta", cta_url_text="Ver tienda", cta_url="tienda.com"
            ),
        )
        self.assertContains(response, "http://")
        self.assertEqual(MessageTemplate.objects.count(), 0)

        self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(
                button_kind="cta",
                cta_url_text="Ver tienda",
                cta_url="https://tienda.com",
            ),
        )
        template = MessageTemplate.objects.get(name="bienvenida_1")
        self.assertEqual(
            template.buttons,
            [{"type": "url", "text": "Ver tienda", "url": "https://tienda.com"}],
        )

    def test_errors_keep_what_was_typed(self):
        response = self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(name="MAYUSCULAS", footer="Mi pie"),
        )
        self.assertContains(response, 'value="MAYUSCULAS"')
        self.assertContains(response, 'value="Mi pie"')
        self.assertContains(response, "Hola {{1}}, bienvenido a {{2}}.")

    def test_sub_type_outside_the_category_falls_back(self):
        self.client.post(
            reverse("plantilla_editor"),
            self.valid_payload(category="authentication", sub_type="carousel"),
        )
        template = MessageTemplate.objects.get(name="bienvenida_1")
        self.assertEqual(template.sub_type, "auth_code")


class MensajeriaPanelEndpointTests(TestCase):
    def test_returns_only_the_panel_fragment(self):
        response = self.client.get(
            reverse("mensajeria_panel", args=["widget-whatsapp"])
        )
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<html", body)
        self.assertNotIn("side-nav", body)  # nav panel is not re-sent
        self.assertIn("Widget de WhatsApp", body)

    def test_every_view_has_a_working_endpoint(self):
        for view in VIEWS:
            with self.subTest(view.key):
                response = self.client.get(reverse("mensajeria_panel", args=[view.key]))
                self.assertEqual(response.status_code, 200)

    def test_unknown_view_is_404(self):
        self.assertEqual(self.client.get("/mensajeria/panel/bogus/").status_code, 404)

    def test_panel_endpoint_matches_what_the_page_embeds(self):
        fragment = self.client.get(
            reverse("mensajeria_panel", args=["plantillas-whatsapp"])
        ).content.decode()
        page = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertIn(fragment.strip(), page)

    def test_nav_rows_target_the_panel(self):
        html = self.client.get(reverse("section", args=["mensajeria"])).content.decode()
        self.assertIn('hx-target="#mensajeria-panel"', html)
        self.assertIn('hx-get="/mensajeria/panel/widget-whatsapp/"', html)
        self.assertIn('hx-push-url="/s/mensajeria/?view=widget-whatsapp"', html)
