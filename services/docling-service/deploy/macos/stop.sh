#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
SERVICE_DIR=${SCRIPT_DIR:h:h}
REPO_ROOT=${SERVICE_DIR:h:h}
RUNTIME_DIR=${REPO_ROOT}/.runtime/docling-release/macos
VENV_DIR=${RUNTIME_DIR}/venv

for service in api backend; do
  pid_file=${RUNTIME_DIR}/pids/${service}.pid
  [[ -f ${pid_file} ]] || continue
  pid=$(<${pid_file})
  if kill -0 ${pid} 2>/dev/null; then
    command_line=$(ps -p ${pid} -o command= 2>/dev/null || true)
    if [[ ${command_line} != *${VENV_DIR}* && ${command_line} != *${SCRIPT_DIR}* ]]; then
      print -u2 "Refusing to stop ${service}: PID ${pid} is not this release."
      continue
    fi
    kill ${pid}
    for _attempt in {1..20}; do
      kill -0 ${pid} 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 ${pid} 2>/dev/null; then
      kill -KILL ${pid}
    fi
  fi
  rm -f ${pid_file}
done
print "docling-service stopped"
