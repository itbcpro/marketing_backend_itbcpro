"""Catalog-based transactional email.

Every sentence is a separate translatable message so copy lives in the message catalog, never
inline. A template is an ordered list of message lines; placeholders are filled with ``format``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.translation import gettext as _

from core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmailTemplate:
    subject: Callable[[], str]
    lines: tuple[Callable[[], str], ...]


TEMPLATES: dict[str, EmailTemplate] = {
    "password_reset": EmailTemplate(
        subject=lambda: _("Reset your password"),
        lines=(
            lambda: _("We received a request to reset your password."),
            lambda: _("Open the link below to choose a new password. It expires in one hour."),
            lambda: "{link}",
            lambda: _("If you did not request this, you can safely ignore this email."),
        ),
    ),
    "invitation": EmailTemplate(
        subject=lambda: _("You have been invited to {workspace}"),
        lines=(
            lambda: _("{inviter} invited you to join {workspace} as {role}."),
            lambda: _("Open the link below to accept the invitation. It expires in {days} days."),
            lambda: "{link}",
        ),
    ),
    "approval_needed": EmailTemplate(
        subject=lambda: _("Approval needed: {summary}"),
        lines=(
            lambda: _("{agent} prepared something in {workspace} that needs your decision."),
            lambda: _("Summary: {summary}"),
            lambda: _("Risk class: {risk}"),
            lambda: _("Open the approvals inbox to review, edit, approve or reject it."),
        ),
    ),
    "crm_digest": EmailTemplate(
        subject=lambda: _("You have {count} unread notifications in {workspace}"),
        lines=(
            lambda: _("Here is what needs your attention in {workspace}:"),
            lambda: "{items}",
            lambda: _("Open the CRM to see everything."),
        ),
    ),
    "verify_email": EmailTemplate(
        subject=lambda: _("Confirm your email address"),
        lines=(
            lambda: _(
                "Thanks for signing up. Confirm your email address to start using your account."
            ),
            lambda: "{link}",
            lambda: _("If you did not sign up, you can safely ignore this email."),
        ),
    ),
    "account_exists": EmailTemplate(
        subject=lambda: _("You already have an account"),
        lines=(
            lambda: _(
                "Someone tried to sign up with this email address, but an account already exists."
            ),
            lambda: _("You can sign in here:"),
            lambda: "{link}",
            lambda: _("If it was not you, no action is needed."),
        ),
    ),
    "account_locked": EmailTemplate(
        subject=lambda: _("Your account was temporarily locked"),
        lines=(
            lambda: _("Several failed sign-in attempts were detected on your account."),
            lambda: _(
                "Sign-in is paused for {minutes} minutes. If this was not you, reset your password."
            ),
        ),
    ),
}


def render(template_key: str, context: dict[str, object]) -> tuple[str, str]:
    template = TEMPLATES[template_key]
    subject = template.subject().format(**context)
    body = "\n\n".join(line().format(**context) for line in template.lines)
    return subject, body


def send_templated_email(to: str, template_key: str, context: dict[str, object]) -> None:
    subject, body = render(template_key, context)
    message = EmailMultiAlternatives(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
    message.send(fail_silently=False)
    log.info("email_sent", template=template_key)
