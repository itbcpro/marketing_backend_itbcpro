"""Add a rate card row (rows are never edited; a new price is a new row from a later date)."""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError
from django.utils import timezone

from crm.usage.models import RateCard


class Command(BaseCommand):
    help = "Add a rate for one channel. Amounts are in micros (millionths of a currency unit)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--channel", required=True)
        parser.add_argument("--currency", required=True)
        parser.add_argument("--cost-micros", type=int, required=True)
        parser.add_argument("--price-micros", type=int, required=True)
        parser.add_argument("--country", default="")
        parser.add_argument("--category", default="")
        parser.add_argument("--from", dest="effective_from", default="", help="ISO date and time")

    def handle(self, *args: object, **options: object) -> None:
        raw = str(options["effective_from"])
        moment = datetime.fromisoformat(raw) if raw else timezone.now()
        if timezone.is_naive(moment):
            moment = timezone.make_aware(moment)
        try:
            row = RateCard.objects.create(
                channel=str(options["channel"]),
                country=str(options["country"]).upper(),
                category=str(options["category"]),
                currency=str(options["currency"]).upper(),
                cost_micros=int(options["cost_micros"]),  # type: ignore[call-overload]
                price_micros=int(options["price_micros"]),  # type: ignore[call-overload]
                effective_from=moment,
            )
        except (IntegrityError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"rate {row.pk} effective from {row.effective_from.isoformat()}")
