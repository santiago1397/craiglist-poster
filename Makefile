.PHONY: help deploy docker-prod docker-prod-down docker-prod-logs docker-prod-build

# === Deploy (push to VPS via SSH) ===
# Requires an SSH alias `craigslist` in ~/.ssh/config (see scripts/deploy.sh).
# Override on the command line if needed:
#   make deploy SSH_ALIAS=myhost VPS_REPO_PATH=/srv/craigslist_automation
deploy:
	@SSH_ALIAS="$(SSH_ALIAS)" VPS_REPO_PATH="$(VPS_REPO_PATH)" bash scripts/deploy.sh

# === Docker: Production (with Traefik) ===
docker-prod:
	docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
	@echo ""
	@echo "Production stack started behind Traefik."
	@echo "Endpoints are configured via API_HOST / WEB_HOST in .env.prod."

docker-prod-down:
	docker compose -f docker-compose.prod.yml down

docker-prod-logs:
	docker compose -f docker-compose.prod.yml logs -f

docker-prod-build:
	docker compose -f docker-compose.prod.yml --env-file .env.prod build

# === Help ===
help:
	@echo ""
	@echo "craigslist_automation - Available Commands"
	@echo "==========================================="
	@echo ""
	@echo "Deploy:"
	@echo "  make deploy             SSH into VPS, git pull, rebuild, restart"
	@echo "                          Override with SSH_ALIAS=... VPS_REPO_PATH=..."
	@echo ""
	@echo "Docker (Production with Traefik):"
	@echo "  make docker-prod        Start production stack (reads .env.prod)"
	@echo "  make docker-prod-down   Stop production stack"
	@echo "  make docker-prod-logs   Tail production logs"
	@echo "  make docker-prod-build  Build production images"
	@echo ""
