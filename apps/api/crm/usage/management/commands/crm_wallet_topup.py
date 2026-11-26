"""Credit an organization's wallet. Repeating the same reference credits it only once."""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from core.errors import DomainError
from crm.usage import wallet
from workspaces.models import Organization


class Command(BaseCommand):
    help = "Add prepaid balance to an organization (amount in micros)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True, help="Organization slug")
        parser.add_argument("--amount-micros", type=int, required=True)
        parser.add_argument(
            "--reference", required=True, help="Payment reference; makes it idempotent"
        )
        parser.add_argument("--note", default="")

    def handle(self, *args: object, **options: object) -> None:
        try:
            organization = Organization.objects.get(slug=str(options["organization"]))
        except Organization.DoesNotExist as exc:
            raise CommandError("unknown organization") from exc
        try:
            entry = wallet.top_up(
                organization,
                amount_micros=int(options["amount_micros"]),  # type: ignore[call-overload]
                reference=str(options["reference"]),
                note=str(options["note"]),
            )
        except DomainError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"balance is now {entry.balance_after_micros} micros")
