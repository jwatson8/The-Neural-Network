#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Python 3.12 is required but was not found." >&2
  echo "Install Python 3.12, then run this script again." >&2
  exit 1
fi

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt

echo "Environment ready. Activate it with: source .venv/bin/activate"
