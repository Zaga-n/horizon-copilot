#!/usr/bin/env bash
set -euo pipefail
uv lock --check
uv run --locked ruff check services libs conftest.py
uv run --locked ruff format --check services libs conftest.py
uv run --locked lint-imports
scripts/mypy-members.sh
uv run --locked pytest --collect-only -q
uv run --locked pytest -m "not integration and not e2e and not live" "$@"
