#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
SERVICE_DIR=${SCRIPT_DIR:h:h}
REPO_ROOT=${SERVICE_DIR:h:h}
RUNTIME_DIR=${REPO_ROOT}/.runtime/docling-release/macos
VENV_DIR=${RUNTIME_DIR}/venv
PYTHON_BIN=${PYTHON_BIN:-python3}
RUNTIME_REQUIREMENTS=${SCRIPT_DIR}/runtime.txt
SHARED_CONSTRAINTS=${SERVICE_DIR}/deploy/docker/backend-constraints.txt
MACOS_CONSTRAINTS=${SCRIPT_DIR}/constraints.txt
export PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT:-180}

if [[ $(uname -s) != Darwin ]]; then
  print -u2 "This installer is for macOS only."
  exit 2
fi

PYTHON_OK=$(${PYTHON_BIN} -c 'import sys; print(int((3, 11) <= sys.version_info[:2] < (3, 14)))')
if [[ ${PYTHON_OK} != 1 ]]; then
  print -u2 "Python 3.11, 3.12, or 3.13 is required. Set PYTHON_BIN explicitly."
  exit 2
fi

mkdir -p ${RUNTIME_DIR}/logs ${RUNTIME_DIR}/pids ${RUNTIME_DIR}/data/{inputs,outputs,state}
${PYTHON_BIN} -m venv ${VENV_DIR}
${VENV_DIR}/bin/python -m pip install --upgrade pip wheel 'setuptools<82'
${VENV_DIR}/bin/python -m pip install \
  --no-build-isolation \
  --constraint ${SHARED_CONSTRAINTS} \
  --constraint ${MACOS_CONSTRAINTS} \
  -e "${SERVICE_DIR}[api,macos]" \
  -r ${RUNTIME_REQUIREMENTS}
# docling-serve declares every remote orchestrator as an extra of its base
# wheel.  The formal local release only needs the dependencies pinned above.
${VENV_DIR}/bin/python -m pip install --no-deps docling-serve==1.20.0
${VENV_DIR}/bin/python -m pip check

MODEL_CACHE=${DOCLING_MODEL_CACHE:-${HOME}/.cache/docling/models}
MODEL_MARKER=${MODEL_CACHE}/.local-ai-lab-models-v2
if [[ ! -f "${MODEL_MARKER}" ]]; then
  print "Initializing Docling models in ${MODEL_CACHE}"
  mkdir -p "${MODEL_CACHE}"
  if [[ $(uname -m) == arm64 ]]; then
    FORMULA_MODEL=granitedocling_mlx
  else
    FORMULA_MODEL=granitedocling
  fi
  ${VENV_DIR}/bin/docling-tools models download \
    layout tableformer code_formula ${FORMULA_MODEL} \
    --output-dir "${MODEL_CACHE}"
  touch "${MODEL_MARKER}"
fi

print "Installed docling-service 1.1.0 into ${VENV_DIR}"
print "Start it with: ${SCRIPT_DIR}/start.sh"
