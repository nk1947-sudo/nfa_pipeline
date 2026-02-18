# NFA Pipeline — Developer Makefile
# ==================================
# Usage: make <target>

.PHONY: help install install-dev test test-verbose coverage lint format typecheck clean run run-dry

PYTHON   := python3
PIP      := pip3
PKG      := nfa_pipeline

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Installation ──────────────────────────────────────────────────────────────

install:  ## Install package (production dependencies only)
	$(PIP) install .

install-dev:  ## Install package with dev dependencies
	$(PIP) install -e ".[dev]"

# ── Testing ───────────────────────────────────────────────────────────────────

test:  ## Run tests with coverage
	$(PYTHON) -m pytest tests/ -v --tb=short --cov=$(PKG) --cov-report=term-missing

test-verbose:  ## Run tests with verbose output
	$(PYTHON) -m pytest tests/ -vvv --tb=long

test-fast:  ## Run tests without coverage (faster)
	$(PYTHON) -m pytest tests/ -v --tb=short

coverage:  ## Generate HTML coverage report
	$(PYTHON) -m pytest --cov=$(PKG) --cov-report=html
	@echo "Open htmlcov/index.html to view coverage"

# ── Code quality ──────────────────────────────────────────────────────────────

lint:  ## Run ruff linter
	$(PYTHON) -m ruff check $(PKG)/ tests/

format:  ## Auto-format with black
	$(PYTHON) -m black $(PKG)/ tests/ scripts/

format-check:  ## Check formatting without changing files
	$(PYTHON) -m black --check $(PKG)/ tests/ scripts/

typecheck:  ## Run mypy type checks
	$(PYTHON) -m mypy $(PKG)/

# ── Pipeline operations ───────────────────────────────────────────────────────

run:  ## Run the full pipeline (2000–2023)
	$(PYTHON) -m $(PKG)

run-dry:  ## Dry run — show what would be scraped
	$(PYTHON) -m $(PKG) --dry-run

run-year:  ## Run for a single year (usage: make run-year YEAR=2015)
	$(PYTHON) -m $(PKG) --years $(YEAR)

status:  ## Show checkpoint status
	$(PYTHON) -m $(PKG) --status

reset:  ## Reset checkpoint (will re-scrape everything)
	$(PYTHON) -m $(PKG) --reset-checkpoint

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:  ## Remove build artifacts and cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned."

clean-data:  ## Remove all scraped data (WARNING: irreversible)
	@echo "WARNING: This will delete all data in data/"
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] && rm -rf data/ && echo "Data deleted." || echo "Aborted."
