.PHONY: install build test eval dimensions run

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'
	npm install --prefix frontend

build:
	npm run build --prefix frontend

test:
	.venv/bin/ruff check backend
	.venv/bin/pytest -q
	npm run build --prefix frontend

eval:
	PYTHONPATH=backend .venv/bin/python scripts/run_eval.py

dimensions:
	PYTHONPATH=backend .venv/bin/python scripts/check_dimensions.py

run: build
	PYTHONPATH=backend .venv/bin/uvicorn sidequest.api:app --reload
