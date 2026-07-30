.PHONY: help install demo warehouse warehouse-clean fixtures examples test lint check clean

PY ?= python
DBT := $(PY) -m dbt.cli.main
WAREHOUSE := demo/warehouse

help:
	@echo "make install    install faultline with dev + demo extras"
	@echo "make demo       run Faultline against the bundled pipeline (no setup)"
	@echo "make warehouse  build the dbt/DuckDB warehouse, both worlds"
	@echo "make fixtures   capture the built warehouse as the demo fixtures"
	@echo "make examples   regenerate examples/ from the fixtures"
	@echo "make test       run the test suite"
	@echo "make lint       ruff"
	@echo "make check      lint + test + demo (what CI runs)"

install:
	$(PY) -m pip install -e ".[dev,demo,agent]"

demo:
	faultline demo --explain

# Builds the pipeline twice from one project: once as it shipped (all four faults) and once
# as its authors believed they built it. The second is the baseline the first is judged
# against, which is how the silent-semantic-change detector has anything to compare.
warehouse:
	cd $(WAREHOUSE) && $(PY) generate_seeds.py
	# Clean first: both builds share one database, and building the faulty world last
	# leaves the tables that demo/measure_skew.py reads in the faulty state.
	cd $(WAREHOUSE) && $(DBT) build --profiles-dir . --full-refresh 		--target-path target-clean --vars '{clean: true}'
	cd $(WAREHOUSE) && $(DBT) docs generate --profiles-dir . 		--target-path target-clean --vars '{clean: true}'
	cd $(WAREHOUSE) && $(DBT) build --profiles-dir . --full-refresh
	cd $(WAREHOUSE) && $(DBT) docs generate --profiles-dir .

fixtures: warehouse
	$(PY) demo/build_fixtures.py

examples:
	faultline demo --report examples/scan-report.md --pr-comment examples/pr-comment.md
	faultline demo-fixture examples/demo-graph.json
	$(PY) demo/build_examples.py

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

check: lint test
	faultline demo >/dev/null && echo "demo OK"

clean:
	rm -rf $(WAREHOUSE)/target $(WAREHOUSE)/target-clean $(WAREHOUSE)/warehouse.duckdb
	rm -rf $(WAREHOUSE)/logs .pytest_cache
