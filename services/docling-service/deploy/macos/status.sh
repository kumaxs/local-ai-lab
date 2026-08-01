#!/bin/zsh
set -euo pipefail

API_PORT=${DOCLING_API_PORT:-8000}
if curl -fsS http://127.0.0.1:${API_PORT}/healthz; then
  print
  exit 0
fi
print -u2 "docling-service is not healthy on port ${API_PORT}"
exit 1
