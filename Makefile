# Ultimate Data Pipeline — realtime toolchain.
# Plain `uv sync` exact-syncs .venv and would remove the S0 model runtime
# (TheRock torch wheels + laya); `make sync` runs --inexact to preserve it.

.PHONY: sync lint fmt typecheck test hooks model up down logs demo load-test

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
	@echo "wired in S5: uv run laya server"

up:
	@echo "wired in S3 (docker compose up -d)"

down:
	@echo "wired in S3 (docker compose down)"

logs:
	@echo "wired in S3 (docker compose logs -f)"

demo:
	@echo "wired in S8 (end-to-end demo run)"

load-test:
	@echo "wired in S8 (synthetic load test)"
