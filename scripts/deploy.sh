#!/usr/bin/env bash
# Deploys the craigslist_automation stack per the pattern in
# /opt/SERVER_SETUP.md. Requires:
#   - `craigslist` alias in ~/.ssh/config
#   - repo cloned at $VPS_REPO_PATH on the VPS
#   - .env.prod present on the VPS at that path (mode 600)
#   - The traefik-public network already created (once per VPS)

set -euo pipefail

SSH_ALIAS="${SSH_ALIAS:-craigslist}"
VPS_REPO_PATH="${VPS_REPO_PATH:-/opt/santiagoproperties/craigslist_automation}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://localhost:8000/health}"
COMPOSE_FILE="docker-compose.prod.yml"

echo "→ Testing SSH alias '$SSH_ALIAS'"
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$SSH_ALIAS" true; then
  echo "!! Cannot reach '$SSH_ALIAS'. Check ~/.ssh/config." >&2
  exit 1
fi

echo "→ Verifying .env.prod exists on the server"
ssh "$SSH_ALIAS" "test -f $VPS_REPO_PATH/.env.prod" || {
  echo "!! $VPS_REPO_PATH/.env.prod missing. Copy .env.prod.example and fill it in." >&2
  exit 1
}

echo "→ git fetch + fast-forward pull"
ssh "$SSH_ALIAS" "cd $VPS_REPO_PATH && git fetch origin && git merge --ff-only origin/master" || {
  echo "!! git pull failed (probably non-fast-forward). Reconcile on the server." >&2
  exit 1
}

echo "→ docker compose build"
ssh "$SSH_ALIAS" "cd $VPS_REPO_PATH && docker compose -f $COMPOSE_FILE --env-file .env.prod build"

echo "→ docker compose up -d"
ssh "$SSH_ALIAS" "cd $VPS_REPO_PATH && docker compose -f $COMPOSE_FILE --env-file .env.prod up -d"

echo "→ Waiting for API healthcheck at $HEALTHCHECK_URL (up to 90s)"
if ssh "$SSH_ALIAS" "for i in \$(seq 1 30); do curl -fsS $HEALTHCHECK_URL >/dev/null 2>&1 && exit 0; sleep 3; done; exit 1"; then
  echo "✓ API healthy"
else
  echo "!! API did not become healthy within 90s. Last 80 log lines:" >&2
  ssh "$SSH_ALIAS" "cd $VPS_REPO_PATH && docker compose -f $COMPOSE_FILE logs --tail=80 api" >&2
  exit 1
fi

echo ""
echo "Deployed. Public URLs:"
echo "  https://$(ssh "$SSH_ALIAS" "grep '^WEB_HOST=' $VPS_REPO_PATH/.env.prod | cut -d= -f2")"
echo "  https://$(ssh "$SSH_ALIAS" "grep '^API_HOST=' $VPS_REPO_PATH/.env.prod | cut -d= -f2")"
echo ""
echo "Tail logs:  ssh $SSH_ALIAS \"cd $VPS_REPO_PATH && docker compose -f $COMPOSE_FILE logs -f\""
