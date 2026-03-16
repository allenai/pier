.PHONY: test lint format typecheck check

UV := uv run --extra dev

test:
	$(UV) pytest

lint:
	$(UV) ruff check .
	$(UV) ruff format --check .

format:
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck:
	$(UV) mypy pier

check:
	$(MAKE) -j lint typecheck test
