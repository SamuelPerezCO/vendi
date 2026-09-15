"""Turn a password into the fingerprint Django stores for it.

Every login lives in the database (see core/agents.py), and Django never
stores a password -- only a salted hash of it. This command prints that hash
on your own computer, so a password can be set straight in the database
without the password itself being shared, pasted into a chat or left in a
shell history:

    python manage.py hashear_clave samuel

It prompts for the password twice, hidden, unless ``--password`` is given,
applies the same floor as the Usuarios dialog, and prints one line starting
with the hasher's name (``pbkdf2_sha256$...``). That line is what goes into
``auth_user.password`` for the account. The usual ways to change a password
are still the Usuarios page, for a master who can sign in, and
``manage.py crear_maestro`` when nobody can.

The username is optional; given, it only lets the floor refuse a password
equal to it.
"""

from getpass import getpass

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

from core import agents


class Command(BaseCommand):
    help = (
        "Genera el hash de una contraseña para guardarlo directamente en la "
        "base de datos, sin compartir la contraseña."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "username",
            nargs="?",
            default="",
            metavar="usuario",
            help="Opcional: el usuario de la cuenta, para rechazar una contraseña igual a él.",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Contraseña. Si la omites se pide por teclado, que es lo recomendable: "
                 "así no queda en el historial del shell.",
        )

    def handle(self, *args, username, password, **options):
        if password is None:
            password = ask_password()
        try:
            agents.validate_password(password, username)
        except agents.WeakPassword as exc:
            raise CommandError(str(exc))
        self.stdout.write(make_password(password))


def ask_password(prompt: str = "Contraseña: ") -> str:
    """Prompt twice, hidden, and refuse a mismatch."""
    first = getpass(prompt)
    second = getpass("Repítela: ")
    if first != second:
        raise CommandError("Las contraseñas no coinciden.")
    return first
