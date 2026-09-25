.PHONY: help dev start test clean

help:
	@echo ""
	@echo "BananaChat: Make Commands"
	@echo "========================"
	@echo ""
	@echo "  make dev     Start development server (auto-setup venv + deps)"
	@echo "  make start   Start production server with Gunicorn"
	@echo "  make test    Run test suite"
	@echo "  make clean   Remove venv, __pycache__, and temp files"
	@echo ""
	@echo "Managed server: sudo ./banana install --mode single --domain ai.example.org"
	@echo "Split deployment: choose --mode web or --mode compute (see docs/deployment.md)"
	@echo "Maintenance: sudo bananachat update / backup / restore / uninstall"
	@echo "Automatic updates are off until: sudo bananachat updates enable"

dev:
	@./dev.sh

start:
	@if [ ! -d ".venv" ]; then \
		python3 -m venv .venv; \
		.venv/bin/pip install -q -r requirements.txt; \
	fi
	@.venv/bin/gunicorn wsgi:app -c gunicorn.conf.py

test:
	@if [ ! -d ".venv" ]; then \
		python3 -m venv .venv; \
	fi
	@.venv/bin/pip install -q -r requirements.txt pytest
	@.venv/bin/python -m pytest tests/ -v

clean:
	@echo "Cleaning..."
	@rm -rf .venv __pycache__ .pytest_cache
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "Done"
