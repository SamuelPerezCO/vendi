"""Create (or restore) a master user straight in the database.

Every login lives in the database (see core/agents.py), and the Usuarios page
that manages them is only open to a master -- so a fresh database, or one
whose masters have all been deactivated or forgotten their passwords, needs a
way in that does not go through the page. This is it:

    python manage.py crear_maestro Samuel --name Samuel

It prompts for the password (twice, hidden) unless ``--password`` is given,
and applies the same floor the Usuarios dialog does. On a username that
already exists it does not fail: it resets that user's password, restores
them if they were deactivated, and makes them a master -- which is what
"get me back in" means when the account is already there. Django admin
accounts (``is_staff``/``is_superuser``) are refused: /admin is a different
door, managed with ``createsuperuser``.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core import agents
from core.management.commands.hashear_clave import ask_password


class Command(BaseCommand):
    help = (
        "Crea un usuario maestro en la base de datos, o le restablece la "
        "contraseña y el rol si ya existe. La vía de entrada en una base nueva "
        "o cuando ningún maestro puede iniciar sesión."
    )

    def add_arguments(self, parser):
        parser.add_argument("username", metavar="usuario")
        parser.add_argument(
            "--name",
            default=None,
            help="Nombre visible; por defecto el usuario (o el nombre que ya tenga).",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Contraseña. Si la omites se pide por teclado, que es lo recomendable: "
                 "así no queda en el historial del shell.",
        )

    def handle(self, *args, username, name, password, **options):
        username = username.strip()
        if not username or any(c in username for c in " :,"):
            raise CommandError("El usuario no puede llevar espacios, dos puntos ni comas.")
        if password is None:
            password = ask_password()
        try:
            agents.validate_password(password, username)
        except agents.WeakPassword as exc:
            raise CommandError(str(exc))

        User = get_user_model()
        user = User.objects.filter(username__iexact=username).first()
        if user is not None and (user.is_staff or user.is_superuser):
            raise CommandError(
                f"{user.username} es una cuenta de Django admin; gestiónala con "
                "createsuperuser / changepassword, no desde aquí."
            )
        if user is None:
            user = agents.create_user(username, password, name or username, master=True)
            self.stdout.write(self.style.SUCCESS(f"Usuario maestro creado: {user.username}."))
            return

        was_inactive = not user.is_active
        user.is_active = True
        user.save(update_fields=["is_active"])
        agents.update_user(user, name or user.first_name or user.username, True, password)
        what = ["contraseña restablecida", "maestro"]
        if was_inactive:
            what.append("restaurado")
        self.stdout.write(
            self.style.SUCCESS(f"Usuario {user.username} actualizado: {', '.join(what)}.")
        )
