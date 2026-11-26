# ADR 0001 — Runtime versions: Python 3.14, Django 6.0, TypeScript 5.9

Status: accepted (2026-09-29)

## Context
The plan proposed Django 6.1 and Python 3.13. Verified during P0.3:
- `django-celery-beat` (needed for database-backed, UI-editable schedules) declares `django<6.1` in every release, so it cannot be installed with Django 6.1.
- Python 3.14 is installed on the developer machine and provides `uuid.uuid7()`, used for time-ordered primary keys.
- The latest TypeScript (7.x, native compiler) is not yet compatible with the peer ranges of `openapi-typescript` and `eslint-config-next`; ESLint 10 is likewise outside the peer range of `eslint-config-next`.

## Decision
Django 6.0.x, Python 3.14, TypeScript 5.9.x, ESLint 9.x. Versions are pinned by lockfiles.

## Consequences
- Upgrade to Django 6.1 when `django-celery-beat` supports it (tracked by the weekly dependency review, P0.10.9 / P0.15.5).
- Re-evaluate TypeScript 7 and ESLint 10 when the Next.js and codegen toolchains declare support.
- Django 6.0 removed `CIEmailField`; emails are stored lower-cased with a unique constraint on `lower(email)`.
