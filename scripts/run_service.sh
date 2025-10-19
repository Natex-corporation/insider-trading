#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
  PROJECT_ROOT="${SCRIPT_DIR}"
else
  PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

source "${PROJECT_ROOT}/.venv/bin/activate"
exec "${PROJECT_ROOT}/.venv/bin/python" "${PROJECT_ROOT}/main.py"
