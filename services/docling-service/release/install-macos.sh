#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
VERSION=$(<${SCRIPT_DIR}/VERSION)
INSTALL_ROOT=${DOCLING_INSTALL_ROOT:-${HOME}/Library/Application Support/Local AI Lab/docling-service/${VERSION}}

if [[ $(uname -s) != Darwin ]]; then
  print -u2 "This installer is for macOS only."
  exit 2
fi

if [[ ${INSTALL_ROOT:A} == ${SCRIPT_DIR:A} ]]; then
  print -u2 "DOCLING_INSTALL_ROOT must differ from the extracted release directory."
  exit 2
fi

mkdir -p "${INSTALL_ROOT:h}"
ditto "${SCRIPT_DIR}" "${INSTALL_ROOT}"

if [[ ${DOCLING_INSTALL_COPY_ONLY:-false} == true ]]; then
  print "Copied Docling Service ${VERSION} to ${INSTALL_ROOT}"
  exit 0
fi

zsh "${INSTALL_ROOT}/services/docling-service/deploy/macos/install.sh"

print ""
print "Docling Service ${VERSION} is installed in ${INSTALL_ROOT}"
print "Start:  zsh '${INSTALL_ROOT}/services/docling-service/deploy/macos/start.sh'"
print "Status: zsh '${INSTALL_ROOT}/services/docling-service/deploy/macos/status.sh'"
print "Stop:   zsh '${INSTALL_ROOT}/services/docling-service/deploy/macos/stop.sh'"
