# Mission Control — developer entry points. Production uses systemd units
# (deploy/systemd/); these targets are for the VPS shell and local dev.

VENV := api/.venv
PY   := $(VENV)/bin/python

.PHONY: setup test db-up db-down migrate migration-head revision dev web-build verify-phase0 verify-phase2

setup:
	python3.12 -m venv $(VENV)
	$(VENV)/bin/pip install -e "api[dev]"

test:
	cd api && $(abspath $(VENV))/bin/pytest

db-up:
	docker compose --env-file .env up -d --wait postgres

db-down:
	docker compose --env-file .env down

migrate:
	cd api && set -a && . ../.env && set +a && $(abspath $(VENV))/bin/alembic upgrade head

migration-head:
	cd api && set -a && . ../.env && set +a && $(abspath $(VENV))/bin/alembic current

revision:
	cd api && set -a && . ../.env && set +a && $(abspath $(VENV))/bin/alembic revision --autogenerate -m "$(m)"

# Local run. / and /v1/* will 401/403 without tailscale serve in front —
# that is the security model working, not a bug. /health answers.
dev:
	cd api && set -a && . ../.env && set +a && $(abspath $(PY)) -m mission_control

web-build:
	cd web && npm ci && npm run build

verify-phase0:
	./scripts/verify-phase0.sh

verify-phase2:
	./scripts/verify-phase2.sh
