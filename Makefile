# Farm List Check: common commands. Needs Node 20+ (for the embedded PostGIS) and uv.
PORT ?= 4121

.PHONY: install run replay test test-live data fixtures reset

install:
	npm ci
	uv sync

run:
	PORT=$(PORT) uv run python -m farmcheck.serve

replay:
	DEMO_REPLAY=1 PORT=$(PORT) uv run python -m farmcheck.serve

test:
	DEMO_REPLAY=1 uv run pytest

test-live:
	LIVE=1 uv run pytest -m live

data:
	uv run python scripts/make_synthetic.py --seed 7

fixtures:
	uv run python scripts/record_fixtures.py

reset:
	rm -rf var/pgdata var/pglite.pid var/pglite.log
