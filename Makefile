# Ultimate Data Pipeline — realtime toolchain.
# Plain `uv sync` exact-syncs .venv and would remove the model runtime
# (TheRock torch wheels + laya); `make sync` runs --inexact to preserve it.

.PHONY: sync lint fmt typecheck test hooks model model-container up up-model up-mock down down-v logs demo load-test mutate

sync:
	uv sync --inexact

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy .

test:
	uv run pytest

hooks:
	uvx prek run -a

model:
	# Host-run Laya server. The decide container reaches it as
	# http://host.docker.internal:8081 (set MODEL_ENDPOINT_URL accordingly),
	# which needs MODEL_HOST=0.0.0.0 (the default binds 127.0.0.1) and, on
	# this box, a ufw rule for the docker subnets:
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

# Demo = the full end-to-end acceptance on the REAL model stack: clean
# slate → up-model → steady 2000 @ 100 ev/s → scam-spike burst → degraded=0,
# delivery + rate-limit and CDC asserts. Phones aren't part of the harness —
# alerts land on the in-network alert-receiver (swap NOTIFY_PROVIDER=telegram
# in .env to watch them arrive on a real device).
demo:
	bash tests/integration/smoke.sh --model

# Load + chaos acceptance (local-only, real model): 10 min @ 100 ev/s with
# a mid-run `docker compose kill` of decide+notify — see
# tests/integration/chaos.sh for the scenario and asserts.
load-test:
	bash tests/integration/chaos.sh

# Deterministic app_users mutations through the conduit bus CDC path
# (postgres → pg_changes → app_users_cdc); prints each op for correlation.
mutate:
	uv run udp-pg-mutate --mode mix --count 10
