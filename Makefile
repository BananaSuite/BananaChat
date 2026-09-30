.PHONY: help dev start test lint clean

PYTHON ?= python3

help:
	@echo ""
	@echo "BananaChat"
	@echo "=========="
	@echo ""
	@echo "  make dev     Development server on http://127.0.0.1:8000 (creates .venv)"
	@echo "  make start   Production server with Gunicorn"
	@echo "  make test    Run the test suite"
	@echo "  make lint    Run ruff"
	@echo "  make clean   Remove .venv and caches"
	@echo ""
	@echo "Managed server: sudo ./banana install --mode single --domain chat.example.org"
	@echo "Split deployment: --mode web and --mode compute (see docs/deployment.md)"
	@echo "Maintenance: sudo bananachat update | backup | restore | status"
	@echo ""

.venv/bin/python:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -q -r requirements.txt

dev:
	@./dev.sh

start: .venv/bin/python
	@.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app

test: .venv/bin/python
	@.venv/bin/python -m pip install -q pytest
	@.venv/bin/python -m pytest -q

lint: .venv/bin/python
	@.venv/bin/python -m pip install -q ruff
	@.venv/bin/python -m ruff check .

clean:
	@rm -rf .venv .pytest_cache .ruff_cache
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
