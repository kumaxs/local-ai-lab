"""Contract constants for the docling-service skeleton."""

STATUS_SUCCESS = "success"
STATUS_FAILED_TIMEOUT = "failed_timeout"
STATUS_FAILED_INVALID_INPUT = "failed_invalid_input"
STATUS_FAILED_UNSUPPORTED_FORMAT = "failed_unsupported_format"
STATUS_FAILED_CONVERSION = "failed_conversion"
STATUS_FAILED_INTERNAL = "failed_internal"

FAILURE_STATUSES = {
    STATUS_FAILED_TIMEOUT,
    STATUS_FAILED_INVALID_INPUT,
    STATUS_FAILED_UNSUPPORTED_FORMAT,
    STATUS_FAILED_CONVERSION,
    STATUS_FAILED_INTERNAL,
}

IMAGE_EXPORT_MODES = {"referenced", "embedded", "placeholder"}

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 300

REQUIRED_SUCCESS_OUTPUTS = [
    "document.html",
    "document.md",
    "document.json",
    "metadata.json",
    "status.json",
]
