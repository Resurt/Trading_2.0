PYTHON ?= python
NPM ?= npm

.PHONY: lint test up down logs frontend-build

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest

frontend-build:
	cd apps/frontend && $(NPM) run build

up:
	@echo "Docker Compose stack will be implemented in step 02. No services started."

down:
	@echo "Docker Compose stack will be implemented in step 02. No services stopped."

logs:
	@echo "Docker Compose logs will be available after step 02."
