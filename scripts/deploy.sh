#!/usr/bin/env bash
# Deploys the craigslist_automation stack per the pattern in
# /opt/SERVER_SETUP.md. Requires:
#   - `dispatch` alias in ~/.ssh/config (override with SSH_ALIAS=...)
#   - repo cloned at $VPS_REPO_PATH on the VPS
#   - .env.prod present on the VPS at that path (mode 600)
#   - The traefik-public network already created (once per VPS)
#
# Migrations are NOT run here. The API container's CMD is
# `alembic upgrade head && uvicorn ...`, so they apply on start and a failed
# migration means a container that never becomes healthy — which this script
# then reports. The version check at the end proves they actually ran.

set -euo pipefail

# The VPS hosts several stacks; `dispatch` is its alias in ~/.ssh/config. The
# repo directory is named after the GitHub repo (craiglist-poster, misspelling
# and all), not after the compose project.
SSH_ALIAS="${SSH_ALIAS:-dispatch}"
VPS_REPO_PATH="${VPS_REPO_PATH:-/opt/santiagoproperties/craiglist-poster}"
API_CONTAINER="${API_CONTAINER:-craigslist_api}"
COMPOSE_FILE="docker-compose.prod.yml"

# ControlMaster multiplexing on this alias is unreliable from Git Bash on
# Windows — a stale socket makes the first connection of a run die with
# "mux_client_request_session: read from master failed". Deploys are a handful
# of connections, so opting out costs nothing and removes a confusing failure.
SSH=(ssh -o ControlMaster=no -o ControlPath=none)

remote() { "${SSH[@]}" "$SSH_ALIAS" "$@"; }
in_repo() { remote "cd $VPS_REPO_PATH && $1"; }
compose() { in_repo "docker compose -f $COMPOSE_FILE --env-file .env.prod $1"; }

echo "→ Testing SSH alias '$SSH_ALIAS'"
if ! "${SSH[@]}" -o BatchMode=yes -o ConnectTimeout=10 "$SSH_ALIAS" true; then
  echo "!! Cannot reach '$SSH_ALIAS'. Check ~/.ssh/config." >&2
  exit 1
fi

echo "→ Verifying the repo and .env.prod exist on the server"
remote "test -d $VPS_REPO_PATH/.git" || {
  echo "!! No git repo at $VPS_REPO_PATH. Set VPS_REPO_PATH=..." >&2
  exit 1
}
remote "test -f $VPS_REPO_PATH/.env.prod" || {
  echo "!! $VPS_REPO_PATH/.env.prod missing. Copy .env.prod.example and fill it in." >&2
  exit 1
}

# A dirty tree means someone edited on the server. --ff-only would fail anyway,
# but failing here says why instead of leaving you to read git's output.
if [ -n "$(in_repo 'git status --porcelain')" ]; then
  echo "!! Working tree on the server is dirty. Commit or discard there first:" >&2
  in_repo "git status --short" >&2
  exit 1
fi

BEFORE_SHA="$(in_repo 'git rev-parse --short HEAD')"
BEFORE_REV="$(remote "docker exec $API_CONTAINER alembic current 2>/dev/null | tail -1" || echo "unknown")"
echo "  currently at $BEFORE_SHA, schema ${BEFORE_REV:-unknown}"

echo "→ git fetch + fast-forward pull"
in_repo "git fetch origin && git merge --ff-only origin/master" || {
  echo "!! git pull failed (probably non-fast-forward). Reconcile on the server." >&2
  exit 1
}

AFTER_SHA="$(in_repo 'git rev-parse --short HEAD')"
if [ "$BEFORE_SHA" = "$AFTER_SHA" ]; then
  echo "  already up to date at $AFTER_SHA — rebuilding anyway"
else
  echo "  $BEFORE_SHA -> $AFTER_SHA"
fi

echo "→ docker compose build"
compose "build"

echo "→ docker compose up -d"
compose "up -d"

# The API is `expose:`d to the Traefik network, not `ports:`-published, so
# curling localhost:8000 from the host fails even on a perfectly good deploy.
# The container already has a healthcheck; ask Docker about it instead.
echo "→ Waiting for '$API_CONTAINER' to report healthy (up to 120s)"
if remote "for i in \$(seq 1 40); do
      s=\$(docker inspect --format '{{.State.Health.Status}}' $API_CONTAINER 2>/dev/null || echo missing)
      [ \"\$s\" = healthy ] && exit 0
      [ \"\$s\" = missing ] && { echo 'container is gone'; exit 1; }
      sleep 3
    done; exit 1"; then
  echo "✓ API healthy"
else
  echo "!! API did not become healthy. Last 80 log lines:" >&2
  compose "logs --tail=80 api" >&2
  echo "" >&2
  echo "   A failed migration shows up here as an alembic traceback before" >&2
  echo "   uvicorn ever starts — the container restarts rather than serving." >&2
  exit 1
fi

# Migrations ran inside the container start command. Report what they did, so a
# schema change is never something you have to remember to check for.
AFTER_REV="$(remote "docker exec $API_CONTAINER alembic current 2>/dev/null | tail -1" || echo "unknown")"
if [ "$BEFORE_REV" = "$AFTER_REV" ]; then
  echo "✓ Schema unchanged (${AFTER_REV:-unknown})"
else
  echo "✓ Schema migrated: ${BEFORE_REV:-unknown} -> ${AFTER_REV:-unknown}"
fi

WEB_HOST="$(remote "grep '^WEB_HOST=' $VPS_REPO_PATH/.env.prod | cut -d= -f2" | tr -d '\r')"
API_HOST="$(remote "grep '^API_HOST=' $VPS_REPO_PATH/.env.prod | cut -d= -f2" | tr -d '\r')"

# Traefik terminates TLS and routes by Host header, so the public URL exercises
# a different path than the container healthcheck. Worth one request.
echo "→ Checking the public endpoint"
if curl -fsS --max-time 15 "https://$API_HOST/health" >/dev/null 2>&1; then
  echo "✓ https://$API_HOST/health responding"
else
  echo "!! Public endpoint not responding — the containers are healthy, so" >&2
  echo "   this points at Traefik routing or DNS, not the app." >&2
fi

echo ""
echo "Deployed $AFTER_SHA."
echo "  https://$WEB_HOST"
echo "  https://$API_HOST"
echo ""
echo "Diagnostics:  https://$WEB_HOST/diagnostics"
echo "Tail logs:    ssh $SSH_ALIAS \"cd $VPS_REPO_PATH && docker compose -f $COMPOSE_FILE logs -f\""
