.PHONY: install dev migrate seed test test-cov lint typecheck eval bench bench-live bench-eval bench-cache-fill bench-regress up down clean

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

# Cache-fill curve. 50000 unique prompts; expected hit rate is 0% across
# every window. A spike means a normalisation or embedding collision bug.
CACHE_FILL_REQUESTS ?= 50000
CACHE_FILL_WINDOW ?= 1000
bench-cache-fill:
	python bench/cache_fill_curve.py --requests $(CACHE_FILL_REQUESTS) --window $(CACHE_FILL_WINDOW)

# Compares two bench-result JSONs and exits non-zero if any tracked metric
# drifts more than 30%. CI runs this with a small fresh sample against a
# committed small-scale baseline.
BENCH_BASELINE ?= bench/results/baseline_1k.json
BENCH_FRESH ?= bench/results/latest.json
BENCH_THRESHOLD ?= 0.30
bench-regress:
	python bench/regress.py $(BENCH_BASELINE) $(BENCH_FRESH) --threshold $(BENCH_THRESHOLD)

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
