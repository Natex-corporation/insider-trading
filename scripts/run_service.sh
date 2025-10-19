#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${PROJECT_ROOT}/.venv/bin/activate"
exec "${PROJECT_ROOT}/.venv/bin/python" "${PROJECT_ROOT}/main.py"
