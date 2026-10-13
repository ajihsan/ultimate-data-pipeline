# Ultimate Data Pipeline — realtime toolchain.
# Plain `uv sync` exact-syncs .venv and would remove the S0 model runtime
# (TheRock torch wheels + laya); `make sync` runs --inexact to preserve it.

.PHONY: sync lint fmt typecheck test hooks model model-container up up-model up-mock down down-v logs demo load-test

sync:
	uv sync --inexact

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy .

test:
	uv run pytest

hooks:
	uvx prek run -a

model:
	# Host-run Laya server (127.0.0.1:8081). NOTE: on this box ufw drops
	# container→host traffic, so the decide container can only reach this if
	# a rule allows the docker subnets:
	#   pkexec ufw allow from 172.16.0.0/12 to any port 8081 proto tcp
	# Otherwise use `make model-container` (in-network, no root needed).
	uv run python -m udp.model_server

model-container:
	docker compose --profile model up -d --wait model

up:
	docker compose up -d --wait

up-model:
	docker compose --profile model up -d --wait

up-mock:
	docker compose -f docker-compose.yaml -f compose.mock.yaml up -d --wait

down:
	docker compose down

down-v:
	docker compose down -v

logs:
	docker compose logs -f --tail 100

demo:
	@echo "wired in S8 (end-to-end demo run)"

load-test:
	@echo "wired in S8 (synthetic load test)"
