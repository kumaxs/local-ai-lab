#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
SERVICE_DIR=${SCRIPT_DIR:h:h}
REPO_ROOT=${SERVICE_DIR:h:h}
RUNTIME_DIR=${REPO_ROOT}/.runtime/docling-release/macos
VENV_DIR=${RUNTIME_DIR}/venv

if [[ ! -x ${VENV_DIR}/bin/docling-serve ]]; then
  print -u2 "Release is not installed. Run ${SCRIPT_DIR}/install.sh first."
  exit 2
fi

mkdir -p ${RUNTIME_DIR}/logs ${RUNTIME_DIR}/pids
for service in backend api; do
  pid_file=${RUNTIME_DIR}/pids/${service}.pid
  if [[ -f ${pid_file} ]] && kill -0 $(<${pid_file}) 2>/dev/null; then
    print -u2 "${service} is already running (PID $(<${pid_file}))."
    exit 2
  fi
done

nohup ${VENV_DIR}/bin/python ${SCRIPT_DIR}/logging_wrapper.py --log-path ${RUNTIME_DIR}/logs/backend.log -- ${SCRIPT_DIR}/run-backend.sh >/dev/null 2>&1 &
print $! >${RUNTIME_DIR}/pids/backend.pid

for attempt in {1..120}; do
  if curl -fsS http://127.0.0.1:${DOCLING_BACKEND_PORT:-5001}/version >/dev/null 2>&1; then
    break
  fi
  if [[ ${attempt} == 120 ]]; then
    print -u2 "Backend did not become ready; inspect ${RUNTIME_DIR}/logs/backend.log"
    zsh ${SCRIPT_DIR}/stop.sh
    exit 1
  fi
  sleep 1
done

nohup ${VENV_DIR}/bin/python ${SCRIPT_DIR}/logging_wrapper.py --log-path ${RUNTIME_DIR}/logs/api.log -- ${SCRIPT_DIR}/run-api.sh >/dev/null 2>&1 &
print $! >${RUNTIME_DIR}/pids/api.pid

for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:${DOCLING_API_PORT:-8000}/healthz >/dev/null 2>&1; then
    print "docling-service is ready at http://127.0.0.1:${DOCLING_API_PORT:-8000}"
    exit 0
  fi
  sleep 1
done

print -u2 "API did not become ready; inspect ${RUNTIME_DIR}/logs/api.log"
zsh ${SCRIPT_DIR}/stop.sh
exit 1
