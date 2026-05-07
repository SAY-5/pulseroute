.PHONY: install dev migrate seed test test-cov lint typecheck eval bench bench-live bench-eval up down clean

REQUESTS ?= 10000

install:
	pip install -e ".[dev]"

dev:
	uvicorn pulseroute_gateway.main:app --reload --host 0.0.0.0 --port 8080

migrate:
	cd services/gateway && alembic upgrade head

seed:
	python scripts/seed.py

test:
	pytest tests/unit -x

test-cov:
	pytest tests/unit --cov --cov-report=term-missing --cov-fail-under=90

test-integration:
	RUN_INTEGRATION=1 pytest tests/integration

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy packages/router/pulseroute_router packages/policies/pulseroute_policies

eval:
	pulseroute-eval run --suite golden --provider fake

bench:
	python bench/bench.py --requests $(REQUESTS)

bench-live:
	bash scripts/bench.sh

# Re-run the golden suite against every wired fake model and refresh the
# committed baseline + a timestamped run artifact under eval/runs/.
bench-eval:
	@mkdir -p eval/baselines eval/runs
	@TS=$$(date -u +%Y%m%dT%H%M%SZ); \
	pulseroute-eval bench --models fake-small,fake-large \
	  --output eval/baselines/golden_v1_fake.json; \
	cp eval/baselines/golden_v1_fake.json eval/runs/$$TS.json; \
	python scripts/gen_pareto_md.py eval/baselines/golden_v1_fake.json eval/baselines/pareto.md; \
	echo "fresh run -> eval/runs/$$TS.json"

up:
	docker compose up -d

down:
	docker compose down -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name *.egg-info -exec rm -rf {} + 2>/dev/null || true
