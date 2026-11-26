# Build from the repository root:  docker build -f apps/api/Dockerfile -t itbcpro-api .
FROM python:3.14-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app/apps/api
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/api/ ./
COPY locale/ /app/locale/
COPY tools/compile_messages.py /app/tools/compile_messages.py
RUN uv run --frozen --no-dev --with polib python /app/tools/compile_messages.py \
    && APP_ENVIRONMENT=test DJANGO_SETTINGS_MODULE=config.settings.test \
       uv run --frozen --no-dev python manage.py collectstatic --noinput

FROM python:3.14-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/apps/api/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod
WORKDIR /app/apps/api

COPY --from=build --chown=app:app /app /app
USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# Web: uvicorn (ASGI). Workers run with: celery -A config.celery worker / beat.
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
