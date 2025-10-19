#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BRANCH_NAME="${SERVICE_BRANCH:-main}"

git -C "${PROJECT_ROOT}" fetch origin "${BRANCH_NAME}"
git -C "${PROJECT_ROOT}" checkout "${BRANCH_NAME}"
git -C "${PROJECT_ROOT}" reset --hard "origin/${BRANCH_NAME}"

source "${PROJECT_ROOT}/.venv/bin/activate"
exec "${PROJECT_ROOT}/.venv/bin/python" "${PROJECT_ROOT}/main.py"
