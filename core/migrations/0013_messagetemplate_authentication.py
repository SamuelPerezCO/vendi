"""The three settings an Autenticación plantilla is made of.

WhatsApp writes an authentication template's copy itself -- the body, the
expiry footer and the security line are its words, localized per language --
and refuses one that arrives carrying its own body text. So a plantilla in
that category stores no copy of its own; it stores these knobs, and
``messaging.providers.meta`` turns them into the fixed component shape the
platform requires.

Existing rows are all Marketing or Utility (the category has never had a
working submission path), so the defaults below apply to nobody and no data
migration is needed.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_storedfile"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagetemplate",
            name="auth_security_recommendation",
            field=models.BooleanField(
                default=True, verbose_name="añadir recomendación de seguridad"
            ),
        ),
        migrations.AddField(
            model_name="messagetemplate",
            name="auth_code_expiration_minutes",
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name="el código vence en (minutos)"
            ),
        ),
        migrations.AddField(
            model_name="messagetemplate",
            name="auth_button_text",
            field=models.CharField(
                blank=True, max_length=25, verbose_name="texto del botón de copiado"
            ),
        ),
    ]
