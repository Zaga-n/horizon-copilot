#!/usr/bin/env bash
set -euo pipefail
for member in services/* libs/*; do
  # Non-Python deployables (for example the frontend) have their own toolchain.
  [[ -f "$member/pyproject.toml" ]] || continue
  roots=("$member/src")
  if [[ -d "$member/tests" ]]; then roots+=("$member/tests"); fi
  MYPYPATH="$member/src:$member/tests" uv run --locked mypy "${roots[@]}"
done
MYPYPATH="." uv run --locked mypy conftest.py
