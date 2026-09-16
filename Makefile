.PHONY: install build places-demo test eval dimensions run

PROBE := data/probes/20260914T041841Z

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'
	npm install --prefix frontend

build:
	npm run build --prefix frontend

# Small offline OSM index from the committed M0 probe; never overwrites a full index.
places-demo:
	@test -f data/places/sydney.sqlite && echo "data/places/sydney.sqlite already exists, left untouched" || \
	PYTHONPATH=backend .venv/bin/python scripts/build_places.py $(PROBE)/harbour.response $(PROBE)/glebe.response $(PROBE)/surry_hills.response --timestamp 2026-09-14T04:18:41Z

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
