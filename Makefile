.PHONY: setup test test-live lint fmt eval run

setup:
	uv sync

test:
	uv run pytest

test-live:
	uv run pytest -m live

lint:
	uv run ruff check src tests evals scripts
	uv run ruff format --check src tests evals scripts

fmt:
	uv run ruff check --fix src tests evals scripts
	uv run ruff format src tests evals scripts

eval:
	uv run python -m evals.run

run:
	uv run retail-agent chat --user $${USER_ID:-alice}
