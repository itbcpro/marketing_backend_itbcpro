"""URL configuration."""

from django.conf import settings
from django.urls import path

from config.api import api
from core import views
from core.metrics import metrics_view
from identity.views import jwks
from workspaces.sse import workspace_events

urlpatterns = [
    path("healthz", views.liveness, name="healthz"),
    path("readyz", views.readiness, name="readyz"),
    path("metrics", metrics_view, name="metrics"),
    path(".well-known/jwks.json", jwks, name="jwks"),
    path("api/v1/events/stream", workspace_events, name="events-stream"),
    path("api/v1/", api.urls),
]

if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path("admin/", admin.site.urls))
