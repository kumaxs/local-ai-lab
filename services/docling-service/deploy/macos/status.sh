#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
SERVICE_DIR=${SCRIPT_DIR:h:h}
REPO_ROOT=${SERVICE_DIR:h:h}
RUNTIME_DIR=${REPO_ROOT}/.runtime/docling-release/macos
VENV_DIR=${RUNTIME_DIR}/venv

if [[ ! -x ${VENV_DIR}/bin/python ]]; then
  print -u2 "Release is not installed. Run ${SCRIPT_DIR}/install.sh first."
  exit 2
fi

exec "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/lifecycle.py" status \
  --runtime-dir "${RUNTIME_DIR}"
