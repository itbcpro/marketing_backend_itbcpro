"""Create a demo workspace with one user per role. Development and staging only."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from identity.models import User
from workspaces.models import Membership, Role, Workspace
from workspaces.services import create_workspace

DEV_PASSWORD = "dev-only-password-change-me-42"  # noqa: S105 - documented in .env.example
OWNER_EMAIL = "owner@itbcpro.test"


class Command(BaseCommand):
    help = "Seed a demo workspace and one user per role (never run in production)."

    def handle(self, *args: object, **options: object) -> None:
        if settings.APP_ENVIRONMENT == "prod":
            raise CommandError("seed_dev must not run in production")

        owner, _ = User.objects.get_or_create(
            email=OWNER_EMAIL, defaults={"full_name": "Demo Owner", "locale": "en"}
        )
        owner.set_password(DEV_PASSWORD)
        owner.save(update_fields=["password"])

        workspace = Workspace.objects.filter(slug="itbcpro").first() or create_workspace(
            name="ITBCPro", owner=owner, slug="itbcpro", reporting_currency="CAD"
        )

        for role in Role:
            if role is Role.OWNER:
                continue
            user, _ = User.objects.get_or_create(
                email=f"{role.value}@itbcpro.test", defaults={"full_name": f"Demo {role.label}"}
            )
            user.set_password(DEV_PASSWORD)
            user.save(update_fields=["password"])
            Membership.objects.update_or_create(
                workspace=workspace, user=user, defaults={"role": role}
            )
        self.stdout.write(
            self.style.SUCCESS(f"Seeded workspace '{workspace.name}' with demo users")
        )
