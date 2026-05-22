"""Request validation for the docling-service skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from .contract import (
    DEFAULT_TIMEOUT_SECONDS,
    IMAGE_EXPORT_MODES,
    MAX_TIMEOUT_SECONDS,
    STATUS_FAILED_INVALID_INPUT,
    STATUS_FAILED_UNSUPPORTED_FORMAT,
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    input_file_path: Path | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    image_export_mode: str = "referenced"
    detected_format: str | None = None


def validate_uuid4(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = UUID(str(value), version=4)
    except (TypeError, ValueError, AttributeError):
        return False
    return str(parsed) == str(value).lower()


def is_remote_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https", "ftp"}


def normalize_timeout_seconds(value: int | str | None) -> tuple[bool, int, str | None]:
    if value is None:
        return True, DEFAULT_TIMEOUT_SECONDS, None
    try:
        timeout_seconds = int(value)
    except (TypeError, ValueError):
        return False, DEFAULT_TIMEOUT_SECONDS, "timeout_seconds must be an integer"
    if timeout_seconds <= 0:
        return False, timeout_seconds, "timeout_seconds must be greater than 0"
    if timeout_seconds > MAX_TIMEOUT_SECONDS:
        return False, timeout_seconds, f"timeout_seconds must be <= {MAX_TIMEOUT_SECONDS}"
    return True, timeout_seconds, None


def validate_request(
    *,
    job_uuid: str | None,
    input_file_path: str | None,
    image_export_mode: str | None = None,
    timeout_seconds: int | str | None = None,
) -> ValidationResult:
    if not validate_uuid4(job_uuid):
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_INVALID_INPUT,
            error_code="invalid_job_uuid",
            error_message="job_uuid is required and must be UUIDv4",
        )

    if not input_file_path:
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_INVALID_INPUT,
            error_code="missing_input_file_path",
            error_message="input_file_path is required",
        )

    if is_remote_url(input_file_path):
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_INVALID_INPUT,
            error_code="remote_url_not_allowed",
            error_message="input_file_path must be a local file path",
        )

    mode = image_export_mode or "referenced"
    if mode not in IMAGE_EXPORT_MODES:
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_INVALID_INPUT,
            error_code="invalid_image_export_mode",
            error_message="image_export_mode must be referenced, embedded, or placeholder",
        )

    timeout_ok, normalized_timeout, timeout_error = normalize_timeout_seconds(timeout_seconds)
    if not timeout_ok:
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_INVALID_INPUT,
            error_code="invalid_timeout_seconds",
            error_message=timeout_error,
            timeout_seconds=normalized_timeout,
            image_export_mode=mode,
        )

    path = Path(input_file_path).expanduser()
    if not path.exists() or not path.is_file():
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_INVALID_INPUT,
            error_code="input_file_not_found",
            error_message="input_file_path must exist and be a local file",
            timeout_seconds=normalized_timeout,
            image_export_mode=mode,
        )

    if path.suffix.lower() != ".pdf":
        return ValidationResult(
            ok=False,
            status=STATUS_FAILED_UNSUPPORTED_FORMAT,
            error_code="unsupported_format",
            error_message="only .pdf is supported by the initial skeleton",
            input_file_path=path.resolve(),
            timeout_seconds=normalized_timeout,
            image_export_mode=mode,
        )

    return ValidationResult(
        ok=True,
        input_file_path=path.resolve(),
        timeout_seconds=normalized_timeout,
        image_export_mode=mode,
        detected_format="pdf",
    )
