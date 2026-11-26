"""Fill a development workspace with fake CRM data so every page can be tried by hand."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from audit.outbox import dispatch_batch
from crm.ops.demo import automation, base, deals, messaging, operations, people
from workspaces.models import Workspace

STEPS = [
    ("people", people.run),
    ("deals", deals.run),
    ("messaging", messaging.run),
    ("automation", automation.run),
    ("operations", operations.run),
]


class Command(BaseCommand):
    help = "Seed fake CRM data into a dev workspace (never run in production)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--workspace", default="itbcpro", help="workspace slug")
        parser.add_argument("--owner", default="ahmad@itbcpro.ca", help="acting member's email")
        parser.add_argument("--only", nargs="*", help="run only these steps")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.APP_ENVIRONMENT == "prod":
            raise CommandError("seed_crm_demo must not run in production")
        workspace = Workspace.objects.filter(slug=options["workspace"]).first()
        if workspace is None:
            raise CommandError(f"no workspace '{options['workspace']}'")
        demo = base.build(workspace, options["owner"])
        for name, step in STEPS:
            if options["only"] and name not in options["only"]:
                continue
            with transaction.atomic():
                step(demo)
            self.stdout.write(f"  {name}: done")
        handled = 0
        while batch := dispatch_batch(500):  # fills the activity timeline from the events
            handled += batch
        self.stdout.write(f"  relayed {handled} events")
        for key, amount in sorted(demo.counts.items()):
            self.stdout.write(f"  created {amount} {key}")
        self.stdout.write(self.style.SUCCESS("Demo data ready"))
