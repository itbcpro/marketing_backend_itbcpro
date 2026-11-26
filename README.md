# ITBCPro Marketing Platform

Agent-operated marketing platform: AI agents plan, create, publish and optimise advertising, website
content and social channels; a human gives commands and approves actions. Read `docs/README.md` for the
full plan and `tasks/` for the phase task lists.

The application, its code and its documentation are English only.

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.14, Django 6.0, Django Ninja, Pydantic |
| Jobs | Celery 5.6 + Redis, database-backed schedules |
| Data | PostgreSQL 18 with pgvector |
| Web | Next.js 16, React 19, Tailwind CSS v4, next-intl |
| Tooling | uv, pnpm, ruff, mypy (strict), import-linter, pytest, Vitest |

## Quick start

Prerequisites: Docker, Python 3.14, [uv](https://docs.astral.sh/uv/). The web app is in the `marketing_front_itbcpro` repository.

```bash
cp .env.example .env            # then fill APP_VAULT_MASTER_KEYS (see comments in the file)
make setup                      # install dependencies, compile message catalogs
make up                         # Postgres (54329) and Redis (16379)
make migrate seed               # schema and demo workspace + users
make dev-api                    # http://127.0.0.1:8001
```

If ports 8001 or 3003 are taken on your machine, run the servers on other ports and set
`APP_CSRF_TRUSTED_ORIGINS` to the web origin (Django rejects cross-origin logins otherwise).

Demo users are `owner@itbcpro.test`, `admin@itbcpro.test`, `approver@itbcpro.test`, `editor@itbcpro.test`,
`analyst@itbcpro.test`, `viewer@itbcpro.test`. The shared development password is defined in
`apps/api/workspaces/management/commands/seed_dev.py` and the command refuses to run in production.

## Quality gates

```bash
make lint typecheck layers test check-i18n check-language
make openapi                    # regenerate packages/api-client, then copy it to the front repo's api-client/
```

CI runs the same gates (`.github/workflows/ci.yml`). Pre-commit hooks are configured in `.pre-commit-config.yaml`.

## Layout

```
apps/api        Django project (core, identity, workspaces, connections, audit + phase apps)
packages/api-client   OpenAPI schema and generated TypeScript types
locale/         Backend message catalog (gettext)
prompts/        Versioned prompt templates
tools/          Repository guards and code generation
docs/, tasks/   Architecture, standards, ADRs, phase task lists
```

## Windows notes

- Start Docker Desktop with `docker desktop start`.
- `next build` and `next dev` need `SWC_NATIVE_BINDING_CACHE` pointing to a directory whose ACL does not grant
  write access to "Authenticated Users" (for example `C:\Users\<you>\.swc-cache`).
