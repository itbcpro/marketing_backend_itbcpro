# ADR 0005 — Django settings module selection

Status: accepted (2026-09-29)

## Context
`config/__init__.py` imports the Celery app so it is always configured. The Celery module originally executed `os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")`. Because the package is imported before any entry point, this silently selected development settings and overrode the production default in `asgi.py`. It was found by running the production container.

## Decision
`config/celery.py` never chooses a settings module. Each entry point chooses explicitly: `manage.py` defaults to dev, `asgi.py` and `wsgi.py` default to prod, and the Docker image sets `DJANGO_SETTINGS_MODULE=config.settings.prod`. Workers started outside Docker must export the variable. A regression test (`config/tests/test_entrypoints.py`) asserts that importing `config` selects nothing.
