#!/usr/bin/env bash
# Runs on the server as the forced command of the CI deploy keys (see setup-server.sh).
# Two repositories call it: the API pipeline sends .env, docker-compose.prod.yml and its image tag;
# the web pipeline sends only its image tag. The stack starts once both images and the config are known.
# Input: a tar stream on stdin containing images.env and ghcr_token, plus optionally .env and docker-compose.prod.yml.
set -Eeuo pipefail
umask 077

ROOT="${DEPLOY_ROOT:-/opt/itbcpro}"
COMPOSE_FILE="$ROOT/docker-compose.prod.yml"
KNOWN="$ROOT/state/known.env"   # latest image names and tags received from either pipeline
LIVE="$ROOT/state/live.env"     # tags of the running stack
KEEP_BACKUPS="${KEEP_BACKUPS:-7}"
mkdir -p "$ROOT/state" "$ROOT/backups" "$ROOT/incoming"
touch "$KNOWN"

exec 9>"$ROOT/state/deploy.lock"
flock -w 600 9 || { echo "another deploy is still running" >&2; exit 75; }

if [[ "${SSH_ORIGINAL_COMMAND:-deploy}" != "deploy" ]]; then
  echo "only the 'deploy' command is allowed" >&2
  exit 64
fi

work="$(mktemp -d "$ROOT/incoming/run.XXXXXX")"
trap 'rm -rf "$work"; docker logout ghcr.io >/dev/null 2>&1 || true' EXIT

cat > "$work/in.tar"
while IFS= read -r name; do
  case "$name" in
    .env|images.env|docker-compose.prod.yml|ghcr_token) ;;
    *) echo "unexpected file in bundle: $name" >&2; exit 65 ;;
  esac
done < <(tar -tf "$work/in.tar")
tar -xf "$work/in.tar" -C "$work" --no-same-owner --no-same-permissions
rm -f "$work/in.tar"
[[ -f "$work/images.env" && -f "$work/ghcr_token" ]] || { echo "bundle needs images.env and ghcr_token" >&2; exit 65; }

get() { grep -E "^$1=" "$2" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
valid_image() { [[ "$1" =~ ^ghcr\.io/[a-z0-9._/-]{1,200}$ ]]; }
valid_tag() { [[ "$1" =~ ^[A-Za-z0-9_.-]{1,128}$ ]]; }
set_known() { { grep -vE "^$1=" "$KNOWN" || true; echo "$1=$2"; } > "$KNOWN.new"; mv "$KNOWN.new" "$KNOWN"; }

registry_user="$(get GHCR_USER "$work/images.env")"
docker login ghcr.io -u "$registry_user" --password-stdin < "$work/ghcr_token" >/dev/null

# Record and pull whatever this pipeline built.
sent_api=false
for part in API WEB; do
  image="$(get "${part}_IMAGE" "$work/images.env")"
  tag="$(get "${part}_TAG" "$work/images.env")"
  [[ -n "$image" ]] || continue
  valid_image "$image" || { echo "invalid image name for $part" >&2; exit 65; }
  set_known "${part}_IMAGE" "$image"
  [[ -n "$tag" ]] || continue
  valid_tag "$tag" || { echo "invalid tag for $part" >&2; exit 65; }
  echo "== pull $image:$tag"
  docker pull "$image:$tag"
  set_known "${part}_TAG" "$tag"
  if [[ "$part" == API ]]; then sent_api=true; fi
done
docker logout ghcr.io >/dev/null 2>&1 || true

if [[ -f "$work/.env" ]]; then install -m 600 "$work/.env" "$ROOT/.env"; sent_api=true; fi
if [[ -f "$work/docker-compose.prod.yml" ]]; then install -m 644 "$work/docker-compose.prod.yml" "$COMPOSE_FILE"; sent_api=true; fi

for key in API_IMAGE WEB_IMAGE API_TAG WEB_TAG; do
  if [[ -z "$(get "$key" "$KNOWN")" ]]; then echo "staged; waiting for $key from the other pipeline"; exit 0; fi
done
[[ -f "$ROOT/.env" && -f "$COMPOSE_FILE" ]] || { echo "staged; waiting for config from the API pipeline"; exit 0; }

compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ROOT/.env.deploy" "$@"; }
{ cat "$ROOT/.env"; cat "$KNOWN"; } > "$ROOT/.env.deploy"
chmod 600 "$ROOT/.env.deploy"

live_api="$(get API_TAG "$LIVE")"; live_web="$(get WEB_TAG "$LIVE")"
rollback() {
  echo "== deploy failed, rolling back" >&2
  if [[ -n "$live_api" && -n "$live_web" ]]; then
    sed -i -e "s/^API_TAG=.*/API_TAG=$live_api/" -e "s/^WEB_TAG=.*/WEB_TAG=$live_web/" "$ROOT/.env.deploy"
    compose up -d --wait api worker beat web || echo "rollback did not become healthy" >&2
  fi
  exit 1
}

if [[ "$sent_api" == false && -n "$live_api" ]]; then
  echo "== web only"
  compose up -d --wait --no-deps web || rollback
else
  echo "== data services"
  compose up -d --wait postgres redis
  if [[ -n "$live_api" ]]; then
    echo "== database backup"
    compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
      > "$ROOT/backups/pre-$(date -u +%Y%m%dT%H%M%SZ).dump"
    ls -1t "$ROOT"/backups/pre-*.dump | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -f --
  fi
  echo "== migrate"
  compose run --rm --no-deps api python manage.py migrate --noinput || rollback
  compose run --rm --no-deps api python manage.py ensure_db_roles || rollback
  echo "== start"
  compose up -d --wait --remove-orphans api worker beat web || rollback
fi

grep -E '^(API|WEB)_TAG=' "$KNOWN" > "$LIVE"
docker image prune -f >/dev/null
echo "== live: $(tr '\n' ' ' < "$LIVE")"
