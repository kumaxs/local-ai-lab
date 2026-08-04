#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="${script_dir}/services/docling-service/deploy/docker/compose.release.yaml"

docker compose -f "${compose_file}" pull
docker compose -f "${compose_file}" up -d
docker compose -f "${compose_file}" ps

echo "Docling Service is starting at http://127.0.0.1:${DOCLING_API_PORT:-8766}"
echo "Initial model downloads can take 10 minutes or more."
