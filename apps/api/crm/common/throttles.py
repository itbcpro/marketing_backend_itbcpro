"""Throttles of the CRM's public endpoints, each with its own scope.

Ninja keeps request history per scope and client address. Every ``AnonRateThrottle`` shares the
scope ``anon``, so busy webhooks would otherwise eat the allowance of sign-in and other strict
limits behind the same address. A scope of their own keeps the counts apart.
"""

from ninja.throttling import AnonRateThrottle


class WebhookThrottle(AnonRateThrottle):
    """Provider callbacks (SMS, voice, Meta, generic webhooks): high volume from few addresses."""

    scope = "crm_webhook"


class PublicLinkThrottle(AnonRateThrottle):
    """Links in customers' hands, such as unsubscribe pages."""

    scope = "crm_public_link"


class TrackingThrottle(AnonRateThrottle):
    """Opens and clicks: many readers behind few addresses, such as a mail provider's proxy."""

    scope = "crm_tracking"
