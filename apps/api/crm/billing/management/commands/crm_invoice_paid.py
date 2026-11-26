"""Record that an invoice was paid. Until payments are connected this is an operator task."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from crm.billing import services
from crm.billing.models import Invoice


class Command(BaseCommand):
    help = "Mark an invoice as paid, by its number."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("number")

    def handle(self, *args: object, **options: object) -> None:
        try:
            invoice = Invoice.objects.get(number=str(options["number"]))
        except (Invoice.DoesNotExist, ValidationError) as exc:
            raise CommandError("invoice not found") from exc
        paid = services.mark_paid(invoice)
        self.stdout.write(f"{paid.number} is {paid.status}")
