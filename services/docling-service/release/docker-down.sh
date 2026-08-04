#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="${script_dir}/services/docling-service/deploy/docker/compose.release.yaml"

docker compose -f "${compose_file}" down
echo "Docling Service stopped; named volumes and job data were preserved."
