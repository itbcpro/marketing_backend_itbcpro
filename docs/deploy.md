# Deployment (GitHub Actions to a Docker host)

Push to `main` runs every CI gate, builds the changed images, pushes them to GHCR and deploys over SSH.
Nothing about the server or the app configuration lives in the repository.

```
push main -> CI gates -> build image -> GHCR -> ssh deploy@server -> deploy.sh (per repo)
                                                                        pull, backup, migrate, up --wait, rollback on failure
```

## Security model

- The SSH key in CI is locked to one command (`command="/opt/itbcpro/bin/deploy.sh",restrict`): no shell, no forwarding.
- The server host key is pinned (`DEPLOY_KNOWN_HOSTS`), so there is no trust on first use.
- The server pulls with the short-lived workflow `GITHUB_TOKEN` (packages: read); no registry password is stored.
- Only the web container is published, on `127.0.0.1`. Postgres, Redis and the API have no host ports.
- Containers run with read-only root filesystems, all capabilities dropped and `no-new-privileges`.
- Third-party actions are pinned to commit SHAs. Images carry provenance and SBOM attestations.
- Secrets exist only in GitHub, in `/opt/itbcpro/.env` (mode 600) and in container environments.

## Two repositories, one stack

| Repo | Builds | Sends to the server |
| --- | --- | --- |
| `marketing_backend_itbcpro` (this one) | API image (api, worker, beat) | API image tag, `docker-compose.prod.yml`, the app env file |
| `marketing_front_itbcpro` | web image | web image tag only |

`deploy.sh` keeps the latest image names and tags it received from either pipeline. The stack starts when both images
and the config are known, so the order of the first two pushes does not matter. Later, a web-only push restarts only
the web container; an API push backs up the database, migrates and restarts everything.

## GitHub configuration

Environment `production` in each repo (add required reviewers for a manual gate). The same SSH key pair and
variables go into both repos (or an organization secret shared with both).

| Kind | Name | Repos | Content |
| --- | --- | --- | --- |
| secret | `DEPLOY_SSH_KEY` | both | private half of the CI key pair (ed25519) |
| secret | `DEPLOY_KNOWN_HOSTS` | both | `ssh-keyscan -t ed25519 <host>` output, verified against the server |
| secret | `DEPLOY_ENV` | backend only | the complete env file, based on `infra/deploy/env.prod.example` |
| variable | `DEPLOY_HOST`, `DEPLOY_USER` | both | server address, `deploy` |
| variable | `DEPLOY_SSH_PORT` | both | optional, default 22 |

Generate the cryptographic values with `infra/deploy/generate_env.py`, merge them into the template, paste the file into
`DEPLOY_ENV`, delete the local copy. To change configuration later, edit the secret and re-run the backend Deploy
workflow. Organization repos also need the `GITLEAKS_LICENSE` secret for the secret-scan action.

## One-time server preparation

Additive only: installs Docker if missing, creates the `deploy` user and `/opt/itbcpro`. It does not touch nginx,
the firewall or existing services.

```
scp infra/deploy/deploy.sh infra/deploy/setup-server.sh ci_deploy_key.pub root@SERVER:/root/itbcpro-setup/
ssh root@SERVER 'bash /root/itbcpro-setup/setup-server.sh /root/itbcpro-setup/ci_deploy_key.pub'
```

After changing `deploy.sh`, copy it to `/opt/itbcpro/bin/deploy.sh` again (the pipeline never replaces it, by design).

## nginx and TLS (host nginx)

```
DOMAIN=app.example.com WEB_HOST_PORT=3011 envsubst '$DOMAIN $WEB_HOST_PORT' \
  < infra/deploy/nginx.conf.template > /etc/nginx/sites-available/$DOMAIN
ln -s /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d $DOMAIN
```

The domain's DNS must already point at the server.

## First deploy and day to day

1. Push the repository to GitHub, set the configuration above.
2. Push both repositories (or run each Deploy workflow manually). Pull requests only run CI.
3. Keep `api-client/` in the front repo in sync with `packages/api-client` here after API changes.

## Rollback and backups

- A failed migration or an unhealthy container restores the previous image tags automatically.
- Before every migration a custom-format dump is written to `/opt/itbcpro/backups` (last 7 kept). Schema changes are
  not undone automatically: restore with `pg_restore` into the postgres container if a migration must be reverted.
- Manual rollback: re-run the Deploy workflow on an older commit.
