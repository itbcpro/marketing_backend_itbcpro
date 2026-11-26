"""Move an organization to another plan. Until payments are connected this is an operator task."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from core.errors import DomainError
from crm.plans import services
from workspaces.models import Workspace


class Command(BaseCommand):
    help = "Set the plan of the organization that owns a workspace."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--workspace", required=True, help="Workspace id")
        parser.add_argument("--plan", required=True, help="Plan key, such as starter")

    def handle(self, *args: object, **options: object) -> None:
        try:
            workspace = Workspace.objects.select_related("organization").get(
                pk=str(options["workspace"])
            )
            organization = services.set_plan(workspace.organization, str(options["plan"]))
        except (Workspace.DoesNotExist, ValueError, ValidationError) as exc:
            raise CommandError("workspace not found") from exc
        except DomainError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{organization.name} is now on {organization.plan_key}")
