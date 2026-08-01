#!/bin/sh
set -eu

model_root="${DOCLING_SERVE_ARTIFACTS_PATH:-/models/docling}"
model_marker="${model_root}/.local-ai-lab-models-v2"

if [ ! -f "${model_marker}" ]; then
    echo "Initializing portable Docling models in ${model_root}"
    mkdir -p "${model_root}"
    docling-tools models download \
        layout tableformer code_formula granitedocling rapidocr \
        --output-dir "${model_root}"
    touch "${model_marker}"
fi

exec "$@"
