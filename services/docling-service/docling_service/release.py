"""Shared release runtime for the macOS and Docker service profiles."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import mimetypes
import os
import platform
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contract import REQUIRED_SUCCESS_OUTPUTS
from .lifecycle import (
    Janitor,
    QuotaManager,
    QuotaPolicy,
    RetentionPolicy,
    open_relative_file,
    safe_delete_tree,
    safe_resolve,
)
from .persistence import RuntimeConfigConflict, SQLiteStore
from .webhook import WebhookDispatcher, validate_callback_url


RELEASE_VERSION = "1.1.1"
TERMINAL_STATES = {"succeeded", "failed", "interrupted"}
LOGGER = logging.getLogger(__name__)

# Restarted queued jobs wait for both conversion dependencies instead of
# immediately replaying an adapter request while the backend is still
# starting.  Keep this policy private and bounded: the public API does not
# expose a second retry queue, and a process restart is the durable retry
# boundary for a job that remains queued.
_QUEUE_RESUME_INITIAL_BACKOFF_SECONDS = 0.25
_QUEUE_RESUME_MAX_BACKOFF_SECONDS = 30.0
_QUEUE_RESUME_HEALTH_TIMEOUT_SECONDS = 1.0
_RECOVERY_INTERRUPTING_STAGE = "recovery_interrupting"


class OutputExpiredError(FileNotFoundError):
    """Raised when output existed but its persisted lifecycle deadline passed."""


# Settings that can be changed safely while the service is running.  The
# environment-backed ReleaseConfig remains the immutable baseline; persisted
# overrides are layered on top and can be removed by sending ``null``.
RUNTIME_CONFIG_SPECS: dict[str, dict[str, Any]] = {
    "input_ttl_seconds": {
        "minimum": 60,
        "maximum": 10 * 365 * 24 * 60 * 60,
        "label": "Input retention",
        "description": "Seconds before uploaded source files are eligible for cleanup.",
    },
    "success_output_ttl_seconds": {
        "minimum": 60,
        "maximum": 10 * 365 * 24 * 60 * 60,
        "label": "Successful output retention",
        "description": "Seconds before successful artifacts are eligible for cleanup.",
    },
    "failed_output_ttl_seconds": {
        "minimum": 60,
        "maximum": 10 * 365 * 24 * 60 * 60,
        "label": "Failed output retention",
        "description": "Seconds before failed/interrupted artifacts are eligible for cleanup.",
    },
    "job_ttl_seconds": {
        "minimum": 60,
        "maximum": 10 * 365 * 24 * 60 * 60,
        "label": "Job record retention",
        "description": "Seconds before terminal job tombstones are eligible for cleanup.",
    },
    "staging_ttl_seconds": {
        "minimum": 60,
        "maximum": 7 * 24 * 60 * 60,
        "label": "Staging retention",
        "description": "Seconds before abandoned staging directories are eligible for cleanup.",
    },
    "temp_ttl_seconds": {
        "minimum": 60,
        "maximum": 7 * 24 * 60 * 60,
        "label": "Temporary retention",
        "description": "Seconds before unprotected temporary uploads are eligible for cleanup.",
    },
    "cleanup_interval_seconds": {
        "minimum": 10,
        "maximum": 24 * 60 * 60,
        "label": "Cleanup interval",
        "description": "Seconds between janitor scans.",
    },
    "idempotency_ttl_seconds": {
        "minimum": 60,
        "maximum": 30 * 24 * 60 * 60,
        "label": "Idempotency retention",
        "description": "Seconds idempotency keys remain replayable.",
    },
    "download_lease_seconds": {
        "minimum": 30,
        "maximum": 24 * 60 * 60,
        "label": "Download lease",
        "description": "Seconds an active download lease protects an artifact.",
    },
}
EDITABLE_RUNTIME_KEYS = tuple(RUNTIME_CONFIG_SPECS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_hosts(name: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            host.strip().casefold()
            for host in os.getenv(name, "").split(",")
            if host.strip()
        )
    )


def _normalize_formula_second_pass_policy(raw: str | None) -> str:
    normalized = (raw or "off").strip().casefold()
    if normalized == "review":
        return "auto"
    if normalized == "apply":
        return "apply-all"
    return normalized


def _resolve_formula_second_pass_route_b(
    config: "ReleaseConfig",
    *,
    job_id: str | None = None,
) -> Path | None:
    """Resolve the Route-B directory for a direct or shared-root layout.

    A configured path may itself be a direct document directory, or a shared
    root containing a directory per job.  A candidate is trusted only when
    its ``document.json`` and ``status.json`` are contained regular files and
    ``status.json`` declares ``ok: true``.  Job-specific resolution also
    requires a contained regular ``metadata.json`` whose ``job_id`` matches.
    Per-job paths are resolved before acceptance and must remain inside the
    configured root.
    """
    configured = config.formula_second_pass_route_b_dir
    if configured is None:
        return None

    configured = configured.expanduser()
    if configured.is_symlink():
        raise ValueError(
            "Configured Route-B root must not be a symlink: "
            f"{configured}"
        )
    configured_root = configured.resolve(strict=False)
    if not configured_root.is_dir():
        return None

    def contained_path(path: Path, *, label: str) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(configured_root)
        except ValueError as exc:
            raise ValueError(
                f"Route-B {label} escapes the configured root: {path}"
            ) from exc
        return resolved

    def is_regular_non_symlink(path: Path, *, label: str) -> bool:
        contained_path(path, label=label)
        try:
            mode = path.lstat().st_mode
        except OSError:
            return False
        return stat.S_ISREG(mode)

    def load_regular_json(path: Path, *, label: str) -> Mapping[str, Any] | None:
        if not is_regular_non_symlink(path, label=label):
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    def has_trusted_artifact(directory: Path, *, direct_fallback: bool) -> bool:
        document = directory / "document.json"
        if not is_regular_non_symlink(document, label="document"):
            return False
        status_payload = load_regular_json(
            directory / "status.json",
            label="status",
        )
        if status_payload is None or status_payload.get("ok") is not True:
            return False

        if direct_fallback or job_id is not None:
            metadata_path = directory / "metadata.json"
            try:
                metadata_path.lstat()
            except FileNotFoundError:
                metadata_exists = False
            except OSError:
                return False
            else:
                metadata_exists = True
            # A document/status pair without provenance metadata is never a
            # trusted Route-B artifact.  Requiring metadata even for the
            # no-job compatibility query keeps availability reporting aligned
            # with the job-aware resolver used by the actual adapter command.
            if not metadata_exists:
                return False
            metadata_payload = load_regular_json(
                metadata_path,
                label="metadata",
            )
            if metadata_payload is None:
                return False
            metadata_job_id = metadata_payload.get("job_id")
            if job_id is not None:
                if metadata_job_id != job_id:
                    return False
            elif metadata_job_id is not None:
                return False
        return True

    if job_id is not None:
        candidate = configured / job_id
        candidate_resolved = candidate.resolve(strict=False)
        try:
            candidate_resolved.relative_to(configured_root)
        except ValueError as exc:
            raise ValueError(
                "Route-B per-job directory escapes the configured root: "
                f"{candidate}"
            ) from exc
        if candidate.is_symlink():
            raise ValueError(
                "Route-B per-job directory must not be a symlink: "
                f"{candidate}"
            )
        if has_trusted_artifact(candidate_resolved, direct_fallback=False):
            # Preserve the configured spelling for command-line compatibility;
            # containment above was checked against the fully resolved path.
            return candidate

    if has_trusted_artifact(configured_root, direct_fallback=True):
        return configured
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_input_file(source: Path, destination: Path) -> None:
    """Move an upload, preserving atomic publication across Docker volumes."""
    try:
        source.replace(destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            with source.open("rb") as source_handle, partial.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(partial, destination)
            source.unlink()
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    # Persist both the file entry and the newly created per-job directory
    # before the SQLite task becomes authoritative.
    _fsync_directory(destination.parent)
    _fsync_directory(destination.parent.parent)
    if source.parent != destination.parent and source.parent.exists():
        _fsync_directory(source.parent)


@dataclass(frozen=True)
class ReleaseConfig:
    """Environment-backed runtime policy shared by the API and CLI."""

    profile: str
    serve_url: str
    adapter_path: Path
    input_root: Path
    output_root: Path
    state_root: Path
    max_upload_bytes: int
    max_concurrent_jobs: int
    conversion_timeout_seconds: int
    image_export_mode: str
    formula_policy: str
    cn_ocr_parity: bool
    api_token: str | None
    formula_ocr_url: str | None = None
    max_concurrent_uploads: int = 2
    input_ttl_seconds: int = 24 * 60 * 60
    success_output_ttl_seconds: int = 7 * 24 * 60 * 60
    failed_output_ttl_seconds: int = 2 * 24 * 60 * 60
    job_ttl_seconds: int = 30 * 24 * 60 * 60
    webhook_delivery_ttl_seconds: int = 7 * 24 * 60 * 60
    staging_ttl_seconds: int = 60 * 60
    temp_ttl_seconds: int = 60 * 60
    cleanup_interval_seconds: int = 5 * 60
    max_pending_jobs: int = 20
    max_output_bytes: int = 5 * 1024 * 1024 * 1024
    max_data_bytes: int = 50 * 1024 * 1024 * 1024
    min_free_bytes: int = 2 * 1024 * 1024 * 1024
    idempotency_ttl_seconds: int = 24 * 60 * 60
    download_lease_seconds: int = 5 * 60
    webhook_max_attempts: int = 6
    webhook_allowed_hosts: tuple[str, ...] = ()
    webhook_allow_private_hosts: bool = False
    max_webhook_subscriptions: int = 100
    formula_second_pass_policy: str = "off"
    formula_second_pass_route_b_dir: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "formula_second_pass_policy",
            _normalize_formula_second_pass_policy(self.formula_second_pass_policy),
        )

    @classmethod
    def from_env(cls) -> "ReleaseConfig":
        profile = os.getenv("DOCLING_RELEASE_PROFILE", "macos").strip().casefold()
        if profile not in {"macos", "docker"}:
            raise ValueError("DOCLING_RELEASE_PROFILE must be macos or docker")
        default_adapter = (
            Path("/opt/docling-quality/quality_parity_adapter.py")
            if profile == "docker"
            else _repo_root()
            / "docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py"
        )
        default_data = (
            Path("/data")
            if profile == "docker"
            else _repo_root() / ".runtime/docling-release"
        )
        if profile == "docker":
            formula_default = "formula_service"
        elif platform.machine() == "arm64":
            formula_default = "granite_mlx"
        else:
            formula_default = "granite_transformers"
        formula_policy = os.getenv("DOCLING_FORMULA_POLICY", formula_default)
        formula_ocr_url = os.getenv("DOCLING_FORMULA_OCR_URL")
        formula_second_pass_policy = _normalize_formula_second_pass_policy(
            os.getenv("DOCLING_FORMULA_SECOND_PASS_POLICY", "off")
        )
        formula_second_pass_route_b_dir = os.getenv(
            "DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR"
        )
        if formula_ocr_url is None and profile == "docker" and formula_policy == "formula_service":
            formula_ocr_url = "http://formula:8001"
        timeout_default = "7200" if profile == "docker" else "3600"
        return cls(
            profile=profile,
            serve_url=os.getenv("DOCLING_SERVE_URL", "http://127.0.0.1:5001").rstrip("/"),
            adapter_path=Path(os.getenv("DOCLING_QUALITY_ADAPTER", str(default_adapter))).resolve(),
            input_root=Path(
                os.getenv("DOCLING_INPUT_ROOT", str(default_data / "inputs"))
            ).expanduser().absolute(),
            output_root=Path(
                os.getenv("DOCLING_OUTPUT_ROOT", str(default_data / "outputs"))
            ).expanduser().absolute(),
            state_root=Path(
                os.getenv("DOCLING_STATE_ROOT", str(default_data / "state"))
            ).expanduser().absolute(),
            max_upload_bytes=int(os.getenv("DOCLING_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024))),
            max_concurrent_jobs=max(1, int(os.getenv("DOCLING_MAX_CONCURRENT_JOBS", "1"))),
            conversion_timeout_seconds=max(
                60,
                int(os.getenv("DOCLING_CONVERSION_TIMEOUT_SECONDS", timeout_default)),
            ),
            image_export_mode=os.getenv("DOCLING_IMAGE_EXPORT_MODE", "embedded"),
            formula_policy=formula_policy,
            formula_second_pass_policy=formula_second_pass_policy,
            formula_second_pass_route_b_dir=Path(
                formula_second_pass_route_b_dir
            ).expanduser().absolute()
            if formula_second_pass_route_b_dir
            else None,
            cn_ocr_parity=_env_bool("DOCLING_CN_OCR_PARITY", profile == "macos"),
            api_token=os.getenv("DOCLING_SERVICE_API_TOKEN") or None,
            formula_ocr_url=formula_ocr_url.rstrip("/") if formula_ocr_url else None,
            max_concurrent_uploads=max(
                1, int(os.getenv("DOCLING_MAX_CONCURRENT_UPLOADS", "2"))
            ),
            input_ttl_seconds=max(60, int(os.getenv("DOCLING_INPUT_TTL_SECONDS", "86400"))),
            success_output_ttl_seconds=max(60, int(os.getenv("DOCLING_SUCCESS_OUTPUT_TTL_SECONDS", "604800"))),
            failed_output_ttl_seconds=max(60, int(os.getenv("DOCLING_FAILED_OUTPUT_TTL_SECONDS", "172800"))),
            job_ttl_seconds=max(60, int(os.getenv("DOCLING_JOB_TTL_SECONDS", "2592000"))),
            webhook_delivery_ttl_seconds=max(60, int(os.getenv("DOCLING_WEBHOOK_DELIVERY_TTL_SECONDS", "604800"))),
            staging_ttl_seconds=max(60, int(os.getenv("DOCLING_STAGING_TTL_SECONDS", "3600"))),
            temp_ttl_seconds=max(60, int(os.getenv("DOCLING_TEMP_TTL_SECONDS", "3600"))),
            cleanup_interval_seconds=max(10, int(os.getenv("DOCLING_CLEANUP_INTERVAL_SECONDS", "300"))),
            max_pending_jobs=max(1, int(os.getenv("DOCLING_MAX_PENDING_JOBS", "20"))),
            max_output_bytes=max(1, int(os.getenv("DOCLING_MAX_OUTPUT_BYTES", str(5 * 1024**3)))),
            max_data_bytes=max(1, int(os.getenv("DOCLING_MAX_DATA_BYTES", str(50 * 1024**3)))),
            min_free_bytes=max(0, int(os.getenv("DOCLING_MIN_FREE_BYTES", str(2 * 1024**3)))),
            idempotency_ttl_seconds=max(60, int(os.getenv("DOCLING_IDEMPOTENCY_TTL_SECONDS", "86400"))),
            download_lease_seconds=max(30, int(os.getenv("DOCLING_DOWNLOAD_LEASE_SECONDS", "300"))),
            webhook_max_attempts=max(1, int(os.getenv("DOCLING_WEBHOOK_MAX_ATTEMPTS", "6"))),
            webhook_allowed_hosts=_env_hosts("DOCLING_WEBHOOK_ALLOWED_HOSTS"),
            webhook_allow_private_hosts=_env_bool("DOCLING_WEBHOOK_ALLOW_PRIVATE_HOSTS", False),
            max_webhook_subscriptions=max(
                1, int(os.getenv("DOCLING_MAX_WEBHOOK_SUBSCRIPTIONS", "100"))
            ),
        )

    def validate(self) -> None:
        if self.image_export_mode not in {"embedded", "referenced", "placeholder"}:
            raise ValueError("DOCLING_IMAGE_EXPORT_MODE is invalid")
        if self.formula_policy not in {
            "granite_mlx",
            "granite_transformers",
            "codeformula_transformers",
            "formula_service",
            "off",
        }:
            raise ValueError("DOCLING_FORMULA_POLICY is invalid")
        if self.profile == "docker" and self.formula_policy == "granite_mlx":
            raise ValueError("Docker profile cannot use the macOS-only MLX formula engine")
        if self.profile == "docker" and self.cn_ocr_parity:
            raise ValueError("Docker profile cannot use the macOS-only OCRMac fallback")
        if self.formula_policy == "formula_service" and not self.formula_ocr_url:
            raise ValueError("formula service policy requires DOCLING_FORMULA_OCR_URL")
        if self.formula_ocr_url:
            parsed = urllib.parse.urlsplit(self.formula_ocr_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"formula", "localhost", "127.0.0.1", "::1"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "DOCLING_FORMULA_OCR_URL must be a local Docker/loopback HTTP endpoint"
                )
        if self.formula_second_pass_policy not in {
            "off",
            "auto",
            "apply-all",
        }:
            raise ValueError("DOCLING_FORMULA_SECOND_PASS_POLICY is invalid")
        if self.formula_second_pass_policy == "apply-all":
            if not self.formula_second_pass_route_b_dir:
                raise ValueError(
                    "DOCLING_FORMULA_SECOND_PASS_POLICY=apply-all requires "
                    "DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR"
                )
            if self.formula_second_pass_route_b_dir.is_symlink():
                raise ValueError(
                    "DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR must not be a symlink"
                )
            if not self.formula_second_pass_route_b_dir.is_dir():
                raise ValueError(
                    "DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR must be an existing directory"
                )
        if not self.adapter_path.is_file():
            raise ValueError(f"quality adapter not found: {self.adapter_path}")
        if self.max_data_bytes < self.max_upload_bytes:
            raise ValueError("DOCLING_MAX_DATA_BYTES must cover DOCLING_MAX_UPLOAD_BYTES")
        if self.max_output_bytes > self.max_data_bytes:
            raise ValueError("DOCLING_MAX_OUTPUT_BYTES cannot exceed DOCLING_MAX_DATA_BYTES")
        if self.max_concurrent_uploads < 1:
            raise ValueError("DOCLING_MAX_CONCURRENT_UPLOADS must be at least 1")
        if self.max_webhook_subscriptions < 1:
            raise ValueError("DOCLING_MAX_WEBHOOK_SUBSCRIPTIONS must be at least 1")

    def ensure_directories(self) -> None:
        for path in (
            self.input_root,
            self.output_root,
            self.state_root,
            self.state_root / "jobs",
            self.staging_root,
            self.temp_root,
        ):
            if path.absolute().is_symlink():
                raise ValueError(f"configured runtime directory must not be a symlink: {path}")
            path.mkdir(parents=True, exist_ok=True)
            if path.absolute().is_symlink() or not path.is_dir():
                raise ValueError(f"configured runtime directory is unsafe: {path}")

    @property
    def database_path(self) -> Path:
        return self.state_root / "control.sqlite3"

    @property
    def staging_root(self) -> Path:
        return self.output_root / ".staging"

    @property
    def temp_root(self) -> Path:
        return self.state_root / "temp"

    def effective_formula_second_pass_policy(self, job_id: str | None = None) -> str:
        """Return the policy that can be applied for one job.

        ``formula_second_pass_route_b_dir`` is allowed to be either a direct
        Route-B document directory or a shared root containing one directory
        per job.  Keep this decision in the same resolver used when building
        the adapter command so status/capability callers cannot disagree with
        the worker runtime.  The optional ``job_id`` preserves the historical
        no-argument helper API while allowing per-job resolution.
        """
        if self.formula_second_pass_policy == "off":
            return "off"
        if _resolve_formula_second_pass_route_b(self, job_id=job_id) is not None:
            return "apply-all"
        return "off"

    def public_capabilities(self) -> dict[str, Any]:
        return {
            "release_version": RELEASE_VERSION,
            "profile": self.profile,
            "accepted_input_formats": ["application/pdf"],
            "formula_engine": (
                "unimernet-small+pp-formulanet-l-guarded"
                if self.formula_policy == "formula_service"
                else self.formula_policy
            ),
            "formula_policy": self.formula_policy,
            "ocr_fallback": "ocrmac" if self.cn_ocr_parity else "portable_auto",
            "table_mode": "accurate_with_cell_matching",
            "semantic_reflow": True,
            "citation_links": True,
            "footnote_links": True,
            "algorithm_indentation": True,
            "code_and_algorithm_emphasis": True,
            "image_export_mode": self.image_export_mode,
            "max_upload_bytes": self.max_upload_bytes,
            "max_concurrent_uploads": self.max_concurrent_uploads,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "max_pending_jobs": self.max_pending_jobs,
            "max_output_bytes": self.max_output_bytes,
            "max_data_bytes": self.max_data_bytes,
            "lifecycle": {
                "input_ttl_seconds": self.input_ttl_seconds,
                "success_output_ttl_seconds": self.success_output_ttl_seconds,
                "failed_output_ttl_seconds": self.failed_output_ttl_seconds,
                "job_ttl_seconds": self.job_ttl_seconds,
            },
            "webhooks_enabled": bool(self.webhook_allowed_hosts),
            "max_webhook_subscriptions": self.max_webhook_subscriptions,
        }


@dataclass
class JobRecord:
    job_id: str
    state: str
    original_name: str
    input_path: str
    output_dir: str
    created_at: str
    input_sha256: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None


def build_adapter_command(
    config: ReleaseConfig,
    record: JobRecord,
    *,
    output_root: Path | None = None,
) -> list[str]:
    resolved_route_b: Path | None = None
    if config.formula_second_pass_policy != "off":
        resolved_route_b = _resolve_formula_second_pass_route_b(
            config,
            job_id=record.job_id,
        )
    if config.formula_second_pass_policy == "apply-all":
        if resolved_route_b is None:
            raise ValueError(
                "Cannot run apply-all formula second pass without a matching "
                "Route-B document directory with trusted status.json and "
                "matching metadata.json"
            )
        formula_second_pass_policy = "apply-all"
    elif config.formula_second_pass_policy == "auto" and resolved_route_b is not None:
        formula_second_pass_policy = "apply-all"
    else:
        formula_second_pass_policy = "off"
    command = [
        sys.executable,
        str(config.adapter_path),
        "--serve-url",
        config.serve_url,
        "--input-file",
        record.input_path,
        "--output-root",
        str(output_root or config.output_root),
        "--job-id",
        record.job_id,
        "--sample-name",
        record.original_name,
        "--timeout-seconds",
        str(config.conversion_timeout_seconds),
        "--image-export-mode",
        config.image_export_mode,
        "--formula-policy",
        config.formula_policy,
        "--formula-second-pass-policy",
        formula_second_pass_policy,
    ]
    normalized_input_sha256 = _normalize_hex_sha256(record.input_sha256)
    if normalized_input_sha256 is not None:
        command.extend(["--expected-input-sha256", normalized_input_sha256])
    if formula_second_pass_policy == "apply-all":
        assert resolved_route_b is not None
        command.extend(
            [
                "--formula-second-pass-route-b-dir",
                str(resolved_route_b),
            ]
        )
    if config.cn_ocr_parity:
        command.append("--cn-ocr-parity")
    if config.formula_ocr_url:
        command.extend(["--formula-ocr-url", config.formula_ocr_url])
    return command


def probe_backend(config: ReleaseConfig, timeout: float = 3.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{config.serve_url}/version", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "version": payload}


def probe_formula_service(config: ReleaseConfig, timeout: float = 3.0) -> dict[str, Any]:
    if config.formula_policy != "formula_service":
        return {"ok": True, "enabled": False}
    if not config.formula_ocr_url:
        return {"ok": False, "enabled": True, "error": "formula OCR URL is missing"}
    try:
        with urllib.request.urlopen(
            f"{config.formula_ocr_url}/healthz", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
        return {
            "ok": False,
            "enabled": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"ok": bool(payload.get("ok")), "enabled": True, "details": payload}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if os.name == "nt":  # pragma: no cover - production profiles are POSIX
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return

    parent = path.parent.absolute()
    if parent.is_symlink():
        raise PermissionError(f"refusing to write through symlink directory: {parent}")
    directory_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_nofollow_path(path: Path) -> str:
    try:
        pre_stat = path.lstat()
    except OSError:
        raise
    if not stat.S_ISREG(pre_stat.st_mode):
        raise ValueError("source.pdf is not a regular file")
    open_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    descriptor = os.open(str(path), open_flags)
    descriptor_owner = descriptor
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError("source.pdf is not a regular file")
        expected_file_signature = (
            pre_stat.st_dev,
            pre_stat.st_ino,
            pre_stat.st_size,
            pre_stat.st_mode,
        )
        opened_signature = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_mode,
        )
        if opened_signature != expected_file_signature:
            raise ValueError("source.pdf changed during verification")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor_owner = None
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            read_stat = os.fstat(handle.fileno())
            if read_stat.st_dev != opened_stat.st_dev:
                raise ValueError("source.pdf changed during verification")
            if read_stat.st_ino != opened_stat.st_ino:
                raise ValueError("source.pdf changed during verification")
            if read_stat.st_size != opened_stat.st_size:
                raise ValueError("source.pdf changed during verification")
            if read_stat.st_mode != opened_stat.st_mode:
                raise ValueError("source.pdf changed during verification")
            final_path_stat = path.lstat()
            if (
                final_path_stat.st_dev != pre_stat.st_dev
                or final_path_stat.st_ino != pre_stat.st_ino
                or final_path_stat.st_size != pre_stat.st_size
                or final_path_stat.st_mode != pre_stat.st_mode
            ):
                raise ValueError("source.pdf changed during verification")
        return digest.hexdigest()
    finally:
        if descriptor_owner is not None:
            os.close(descriptor_owner)


def _normalize_hex_sha256(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lower = value.lower()
    if len(lower) != 64:
        return None
    if any(character not in "0123456789abcdef" for character in lower):
        return None
    return lower


class DownloadLease:
    """Adapter used by streaming responses to keep outputs alive."""

    def __init__(self, store: SQLiteStore, lease_id: str, ttl_seconds: int) -> None:
        self._store = store
        self._lease_id = lease_id
        self._ttl_seconds = ttl_seconds
        self._released = False

    def renew(self) -> None:
        if not self._released:
            self._store.renew_download_lease(
                self._lease_id, ttl_seconds=self._ttl_seconds
            )

    @property
    def renew_interval_seconds(self) -> float:
        return min(30.0, max(1.0, self._ttl_seconds / 2))

    def release(self) -> None:
        if not self._released:
            self._store.release_download_lease(self._lease_id)
            self._released = True

    def cancel(self) -> None:
        self.release()


class JobManager:
    """SQLite-backed conversion queue with staged, verified publication."""

    def __init__(
        self,
        config: ReleaseConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        config.validate()
        config.ensure_directories()
        # Keep this object immutable forever as the environment baseline.  The
        # public ``config`` attribute is an effective dataclass rebuilt from
        # that baseline plus validated persisted overrides.
        self._environment_config = config
        self.config = config
        self._runner = runner
        self._lock = threading.RLock()
        self.store = SQLiteStore(
            config.database_path,
            input_root=config.input_root,
            output_root=config.output_root,
            max_pending=config.max_pending_jobs,
            max_data_bytes=config.max_data_bytes,
            webhook_max_attempts=config.webhook_max_attempts,
            max_webhook_subscriptions=config.max_webhook_subscriptions,
        )
        persisted_runtime = self.store.runtime_config_snapshot()
        self._runtime_config_revision = int(persisted_runtime.get("revision", 0))
        self._runtime_overrides = self._validated_runtime_overrides(
            persisted_runtime.get("overrides", {})
        )
        self.config = replace(self._environment_config, **self._runtime_overrides)
        # In-process submission claims keep the normal API path and restart
        # recovery path from enqueueing the same future twice.  The set is
        # deliberately ephemeral: SQLite remains authoritative across
        # process restarts, while a fresh process reconstructs its claims from
        # queued/running state during recovery.
        self._inflight_job_ids: set[str] = set()
        self._resume_pending_ids: set[str] = set()
        self._resume_stop = threading.Event()
        self._resume_wake = threading.Event()
        self._resume_thread: threading.Thread | None = None
        self._stopping = False
        self._pending_input_ids: set[str] = set()
        self._active_temp_names: set[str] = set()
        self.store.import_legacy_state_jobs(
            config.state_root,
            runtime_config_revision=self._runtime_config_revision,
            input_ttl_seconds=self.config.input_ttl_seconds,
            success_output_ttl_seconds=self.config.success_output_ttl_seconds,
            failed_output_ttl_seconds=self.config.failed_output_ttl_seconds,
            job_ttl_seconds=self.config.job_ttl_seconds,
        )
        self._quota = QuotaManager(
            QuotaPolicy(
                max_pending=config.max_pending_jobs,
                max_data_bytes=config.max_data_bytes,
                min_free_bytes=config.min_free_bytes,
                max_output_bytes=config.max_output_bytes,
            )
        )
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_jobs,
            thread_name_prefix="docling-release",
        )
        self._recover_interrupted_jobs()
        self._janitor = Janitor(
            self.store,
            retention=RetentionPolicy(
                input_ttl=self.config.input_ttl_seconds,
                success_output_ttl=self.config.success_output_ttl_seconds,
                failed_output_ttl=self.config.failed_output_ttl_seconds,
                tombstone_ttl=self.config.job_ttl_seconds,
                staging_ttl=self.config.staging_ttl_seconds,
                temp_ttl=self.config.temp_ttl_seconds,
            ),
            input_root=config.input_root,
            output_root=config.output_root,
            tombstone_root=config.state_root / "jobs",
            staging_root=config.staging_root,
            temp_root=config.temp_root,
            download_lease=self.store.has_active_download,
            scan_interval_seconds=self.config.cleanup_interval_seconds,
            pending_inputs=self._pending_inputs_snapshot,
            protected_temp_entries=self._active_temp_snapshot,
            maintenance=(
                self.store.purge_expired_download_leases,
                self.store.purge_expired_idempotency_keys,
                lambda: self.store.purge_webhook_deliveries(
                    max_age_seconds=self.config.webhook_delivery_ttl_seconds
                ),
            ),
        )
        self._janitor.start()
        self._dispatcher: WebhookDispatcher | None = None
        if config.webhook_allowed_hosts:
            private_hosts = (
                set(config.webhook_allowed_hosts)
                if config.webhook_allow_private_hosts
                else set()
            )
            self._dispatcher = WebhookDispatcher(
                self.store,
                allowed_hosts=set(config.webhook_allowed_hosts),
                allow_private_hosts=private_hosts,
                max_attempts_default=config.webhook_max_attempts,
                max_age_seconds=config.webhook_delivery_ttl_seconds,
            )
            self._dispatcher.start()
        self._start_queued_resume_worker()

    def _state_path(self, job_id: str) -> Path:
        return self.config.state_root / "jobs" / f"{job_id}.json"

    @staticmethod
    def _validate_runtime_value(key: str, value: Any) -> int | None:
        if key not in RUNTIME_CONFIG_SPECS:
            raise ValueError(f"runtime configuration key is not editable: {key}")
        if value is None:
            return None
        if isinstance(value, bool) or type(value) is not int:
            raise ValueError(f"{key} must be an integer or null")
        spec = RUNTIME_CONFIG_SPECS[key]
        minimum = int(spec["minimum"])
        maximum = int(spec["maximum"])
        if value < minimum or value > maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return value

    @classmethod
    def _validated_runtime_overrides(cls, overrides: Any) -> dict[str, int]:
        if not isinstance(overrides, Mapping):
            return {}
        valid: dict[str, int] = {}
        for key, value in overrides.items():
            # Invalid persisted values are ignored rather than allowed to
            # poison service startup.  API writes are strict and fail before
            # reaching the store.
            if key not in RUNTIME_CONFIG_SPECS:
                continue
            try:
                normalized = cls._validate_runtime_value(str(key), value)
            except ValueError:
                continue
            if normalized is not None:
                valid[str(key)] = normalized
        return valid

    def _reconfigure_janitor_locked(self) -> None:
        """Apply the effective lifecycle policy to the live janitor.

        Caller must hold ``self._lock``.  The Janitor itself synchronizes its
        policy and only wakes its wait; no cleanup is run inline.
        """
        if not hasattr(self, "_janitor"):
            return
        self._janitor.reconfigure(
            retention=RetentionPolicy(
                input_ttl=self.config.input_ttl_seconds,
                success_output_ttl=self.config.success_output_ttl_seconds,
                failed_output_ttl=self.config.failed_output_ttl_seconds,
                tombstone_ttl=self.config.job_ttl_seconds,
                staging_ttl=self.config.staging_ttl_seconds,
                temp_ttl=self.config.temp_ttl_seconds,
            ),
            scan_interval_seconds=self.config.cleanup_interval_seconds,
        )

    def _refresh_runtime_config_locked(self) -> dict[str, Any]:
        persisted = self.store.runtime_config_snapshot()
        revision = int(persisted.get("revision", self._runtime_config_revision))
        if revision != self._runtime_config_revision:
            self._runtime_config_revision = revision
            self._runtime_overrides = self._validated_runtime_overrides(
                persisted.get("overrides", {})
            )
            self.config = replace(self._environment_config, **self._runtime_overrides)
            self._reconfigure_janitor_locked()
        return persisted

    def runtime_config_snapshot(self) -> dict[str, Any]:
        """Return the redacted runtime policy snapshot used by the Web UI."""
        with self._lock:
            persisted = self._refresh_runtime_config_locked()
            revision = int(persisted.get("revision", self._runtime_config_revision))

            editable: dict[str, dict[str, Any]] = {}
            for key in EDITABLE_RUNTIME_KEYS:
                spec = RUNTIME_CONFIG_SPECS[key]
                effective = int(getattr(self.config, key))
                environment_value = int(getattr(self._environment_config, key))
                editable[key] = {
                    "value": effective,
                    "environment_value": environment_value,
                    "overridden": key in self._runtime_overrides,
                    "minimum": int(spec["minimum"]),
                    "maximum": int(spec["maximum"]),
                    "unit": "seconds",
                    "label": str(spec["label"]),
                    "description": str(spec["description"]),
                    "requires_restart": False,
                }

            # Never include api_token (or any token-bearing path) in this
            # payload.  A configured boolean is sufficient for operators.
            readonly_values: dict[str, tuple[Any, str]] = {
                "max_upload_bytes": (
                    int(self._environment_config.max_upload_bytes),
                    "environment-managed upload limit",
                ),
                "max_concurrent_uploads": (
                    int(self._environment_config.max_concurrent_uploads),
                    "environment-managed upload concurrency",
                ),
                "max_concurrent_jobs": (
                    int(self._environment_config.max_concurrent_jobs),
                    "environment-managed worker concurrency",
                ),
                "min_free_bytes": (
                    int(self._environment_config.min_free_bytes),
                    "environment-managed free-space guard",
                ),
                "webhook_max_attempts": (
                    int(self._environment_config.webhook_max_attempts),
                    "environment-managed retry limit",
                ),
                "webhook_delivery_ttl_seconds": (
                    int(self._environment_config.webhook_delivery_ttl_seconds),
                    "not editable while the service is running",
                ),
                "api_token_configured": (
                    bool(self._environment_config.api_token),
                    "only whether a token is configured is exposed",
                ),
            }
            readonly = {
                key: {"value": value, "reason": reason}
                for key, (value, reason) in readonly_values.items()
            }
            return {
                "revision": revision,
                "updated_at": persisted.get("updated_at"),
                "server_time": utc_now(),
                "existing_job_expiries_unchanged": True,
                "editable": editable,
                "readonly": readonly,
            }

    def update_runtime_config(
        self,
        expected_revision: int,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply a strict runtime override patch using optimistic CAS."""
        if isinstance(expected_revision, bool) or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if not isinstance(changes, Mapping):
            raise ValueError("changes must be an object")

        with self._lock:
            persisted = self.store.runtime_config_snapshot()
            current_revision = int(persisted.get("revision", 0))
            if current_revision != expected_revision:
                raise RuntimeConfigConflict(
                    f"runtime configuration revision {current_revision} does not match {expected_revision}"
                )
            current_overrides = self._validated_runtime_overrides(
                persisted.get("overrides", {})
            )
            for raw_key, raw_value in changes.items():
                key = str(raw_key)
                normalized = self._validate_runtime_value(key, raw_value)
                if normalized is None:
                    current_overrides.pop(key, None)
                else:
                    current_overrides[key] = normalized

            stored = self.store.update_runtime_config(
                expected_revision,
                current_overrides,
            )
            self._runtime_config_revision = int(stored.get("revision", expected_revision + 1))
            self._runtime_overrides = dict(current_overrides)
            self.config = replace(self._environment_config, **self._runtime_overrides)

            # Apply only the components whose runtime policy changed.  The
            # janitor's reconfigure wakes its wait but deliberately does not
            # invoke run_once, so a PATCH cannot trigger an immediate purge.
            self._reconfigure_janitor_locked()
            return self.runtime_config_snapshot()

    def _pending_inputs_snapshot(self) -> set[str]:
        with self._lock:
            return set(self._pending_input_ids)

    def _mark_pending_input(self, job_id: str) -> None:
        with self._lock:
            self._pending_input_ids.add(job_id)

    def _clear_pending_input(self, job_id: str) -> None:
        with self._lock:
            self._pending_input_ids.discard(job_id)

    def _active_temp_snapshot(self) -> set[str]:
        with self._lock:
            return set(self._active_temp_names)

    def protect_temp_file(self, path: Path) -> None:
        if path.parent.resolve() != self.config.temp_root.resolve():
            raise PermissionError("temporary upload is outside DOCLING_STATE_ROOT/temp")
        with self._lock:
            self._active_temp_names.add(path.name)

    def release_temp_file(self, path: Path) -> None:
        with self._lock:
            self._active_temp_names.discard(path.name)

    def _submit_job_once(self, job_id: str) -> bool:
        """Submit one queued job through the shared in-process claim set.

        A queued row is intentionally not transitioned here.  ``_run`` owns
        the durable queued→running transition, so a submit failure leaves the
        row replayable on the next health-gated retry or process restart.
        Holding the manager lock while registering the future closes the race
        between the API submit path, the restart resumer, and shutdown.
        """
        with self._lock:
            if self._stopping or job_id in self._inflight_job_ids:
                return False
            current = self.store.get_job(job_id)
            if not current or current.get("state") != "queued":
                return False
            self._inflight_job_ids.add(job_id)
            try:
                self._executor.submit(self._run_tracked, job_id)
            except BaseException:
                self._inflight_job_ids.discard(job_id)
                raise
            return True

    def _run_tracked(self, job_id: str) -> None:
        """Run a claimed future and release its in-process claim."""
        run_failed = False
        requeue = False
        try:
            self._run(job_id)
        except BaseException:
            # ``_run`` can commit a terminal SQLite state and then fail while
            # writing the legacy mirror.  Keep the original future failure
            # visible to the executor while letting the finally block retry
            # that compatibility write below.
            run_failed = True
            raise
        finally:
            with self._lock:
                self._inflight_job_ids.discard(job_id)
                try:
                    current = self.store.get_job(job_id)
                except Exception:
                    current = None
                if run_failed and current and current.get("state") in TERMINAL_STATES:
                    # The immediate mirror retry is safe even while shutdown
                    # is draining the executor: SQLite has already committed
                    # the terminal state, and no new work is submitted here.
                    # Only retaining a long-lived pending marker is gated by
                    # ``_stopping``.
                    mirror_retry_failed = False
                    try:
                        self._mirror_job(job_id)
                    except Exception:
                        mirror_retry_failed = True
                        LOGGER.warning(
                            "legacy mirror retry failed for terminal job %s",
                            job_id,
                            exc_info=True,
                        )
                    if mirror_retry_failed and not self._stopping:
                        self._resume_pending_ids.add(job_id)
                        self._resume_wake.set()
                        requeue = True
                elif not self._stopping and current and current.get("state") == "queued":
                    self._resume_pending_ids.add(job_id)
                    self._resume_wake.set()
                    requeue = True
            if requeue:
                self._start_queued_resume_worker()

    def _dependencies_ready_for_resume(self) -> bool:
        """Return whether both conversion dependencies pass their health probes."""
        try:
            backend = probe_backend(
                self.config,
                timeout=_QUEUE_RESUME_HEALTH_TIMEOUT_SECONDS,
            )
            if not bool(backend.get("ok")):
                return False
            formula = probe_formula_service(
                self.config,
                timeout=_QUEUE_RESUME_HEALTH_TIMEOUT_SECONDS,
            )
            return bool(formula.get("ok"))
        except Exception:
            # Health probes are advisory and must never terminate the restart
            # worker.  The next bounded-backoff iteration retries them.
            return False

    def _resume_loop(self) -> None:
        delay = _QUEUE_RESUME_INITIAL_BACKOFF_SECONDS
        while not self._resume_stop.is_set():
            try:
                with self._lock:
                    pending_markers: list[tuple[int, int, str, str]] = []
                    for job_id in self._resume_pending_ids:
                        record = self.store.get_job(job_id) or {}
                        queue_position = record.get("queue_position")
                        if (
                            record.get("state") == "queued"
                            and isinstance(queue_position, int)
                            and not isinstance(queue_position, bool)
                            and queue_position > 0
                        ):
                            # ``queue_position`` is derived from the durable,
                            # monotonic queue_order.  Timestamps and UUIDs can
                            # tie or disagree with submission order, so they
                            # must not be used to dispatch queued work.
                            pending_markers.append((0, queue_position, "", job_id))
                        else:
                            # Terminal mirror retries and malformed legacy
                            # rows have no queue position.  Keep their order
                            # deterministic without placing them ahead of
                            # valid queued work.
                            pending_markers.append(
                                (
                                    1,
                                    0,
                                    str(record.get("created_at") or ""),
                                    job_id,
                                )
                            )
                    pending = tuple(
                        job_id
                        for _kind, _position, _created_at, job_id in sorted(
                            pending_markers
                        )
                    )
                    stopping = self._stopping
            except Exception:
                # A transient SQLite/I/O failure while ordering the pending
                # set must not kill the daemon thread and strand queued work.
                LOGGER.warning("queued resume scan failed", exc_info=True)
                self._resume_wake.wait(delay)
                self._resume_wake.clear()
                delay = min(
                    _QUEUE_RESUME_MAX_BACKOFF_SECONDS,
                    max(_QUEUE_RESUME_INITIAL_BACKOFF_SECONDS, delay * 2),
                )
                continue
            if stopping:
                return
            if not pending:
                # Keep an already-started worker alive while idle.  A submit
                # failure can add a new pending id in the tiny interval where
                # a worker would otherwise observe an empty set and exit; the
                # wake event lets that producer hand the work back without a
                # stranded queued row.
                self._resume_wake.wait(_QUEUE_RESUME_MAX_BACKOFF_SECONDS)
                self._resume_wake.clear()
                continue

            if not self._dependencies_ready_for_resume():
                self._resume_wake.wait(delay)
                self._resume_wake.clear()
                delay = min(
                    _QUEUE_RESUME_MAX_BACKOFF_SECONDS,
                    max(_QUEUE_RESUME_INITIAL_BACKOFF_SECONDS, delay * 2),
                )
                continue

            retry_needed = False
            for job_id in pending:
                if self._resume_stop.is_set():
                    return
                try:
                    with self._lock:
                        if job_id not in self._resume_pending_ids:
                            continue
                        current = self.store.get_job(job_id)
                except Exception:
                    LOGGER.warning(
                        "queued resume job lookup failed for %s",
                        job_id,
                        exc_info=True,
                    )
                    retry_needed = True
                    continue
                if not current:
                    with self._lock:
                        self._resume_pending_ids.discard(job_id)
                    continue
                if current.get("state") != "queued":
                    # Finalization is durable in SQLite, while the legacy
                    # state JSON is a compatibility mirror.  A mirror write
                    # can fail after the terminal transition has committed;
                    # retry it before dropping the pending marker so a
                    # transient filesystem error cannot leave a stale queued
                    # mirror forever.
                    if current.get("state") in TERMINAL_STATES:
                        try:
                            self._mirror_job(job_id)
                        except Exception:
                            retry_needed = True
                            continue
                    with self._lock:
                        self._resume_pending_ids.discard(job_id)
                    continue

                # Re-check the clean-input contract immediately before
                # dispatch.  This closes the window in which another process
                # or operator could create a partial staging/output tree while
                # this process was waiting for dependencies to become ready.
                try:
                    expected_input_sha256 = _normalize_hex_sha256(
                        current.get("input_sha256")
                    )
                    recovered_source, recovered_manifest, partial = (
                        self._inspect_recovery_artifacts(
                            job_id,
                            expected_input_sha256=expected_input_sha256,
                        )
                    )
                except Exception:
                    LOGGER.warning(
                        "queued resume artifact inspection failed for %s",
                        job_id,
                        exc_info=True,
                    )
                    retry_needed = True
                    continue
                if recovered_source is not None and not partial:
                    try:
                        assert recovered_manifest is not None
                        self._finalize_recovered_success(
                            current,
                            recovered_source,
                            recovered_manifest,
                        )
                        self._mirror_job(job_id)
                    except Exception:
                        retry_needed = True
                        continue
                    else:
                        with self._lock:
                            self._resume_pending_ids.discard(job_id)
                    continue

                try:
                    replayable = self._queued_job_is_replayable(current)
                except Exception:
                    LOGGER.warning(
                        "queued resume input validation failed for %s",
                        job_id,
                        exc_info=True,
                    )
                    retry_needed = True
                    continue
                if partial or not replayable:
                    try:
                        self._finalize_recovered_interrupted(current)
                        self._mirror_job(job_id)
                    except Exception:
                        # Keep the queued id for a later bounded retry if a
                        # transient SQLite/filesystem error prevents the
                        # interruption record from converging.
                        retry_needed = True
                        continue
                    else:
                        with self._lock:
                            self._resume_pending_ids.discard(job_id)
                    continue

                try:
                    submitted = self._submit_job_once(job_id)
                except Exception:
                    # Keep the durable queued row and retry after a bounded
                    # delay.  This covers a transient executor submission
                    # failure without creating a second future.
                    submitted = False
                    retry_needed = True
                    # Preserve FIFO when the oldest pending job cannot yet
                    # acquire a future; later jobs must not leapfrog it.
                    break
                if submitted:
                    with self._lock:
                        self._resume_pending_ids.discard(job_id)
                else:
                    retry_needed = True
                    break

            with self._lock:
                still_pending = bool(self._resume_pending_ids)
            if still_pending:
                if retry_needed:
                    delay = min(
                        _QUEUE_RESUME_MAX_BACKOFF_SECONDS,
                        max(_QUEUE_RESUME_INITIAL_BACKOFF_SECONDS, delay * 2),
                    )
                else:
                    delay = _QUEUE_RESUME_INITIAL_BACKOFF_SECONDS
                self._resume_wake.wait(delay)
                self._resume_wake.clear()

    def _start_queued_resume_worker(self) -> None:
        with self._lock:
            if self._stopping or not self._resume_pending_ids:
                return
            if self._resume_thread is not None and self._resume_thread.is_alive():
                self._resume_wake.set()
                return
            self._resume_stop.clear()
            self._resume_wake.clear()
            self._resume_thread = threading.Thread(
                target=self._resume_loop,
                daemon=True,
                name="docling-queued-resume",
            )
            self._resume_thread.start()

    def _write_record(self, record: JobRecord) -> None:
        _atomic_json(self._state_path(record.job_id), asdict(record))

    @staticmethod
    def _record_from_mapping(record: Mapping[str, Any]) -> JobRecord:
        fields = {field: record.get(field) for field in SQLiteStore.LEGACY_FIELDS}
        fields["input_sha256"] = record.get("input_sha256")
        return JobRecord(**fields)

    def _mirror_job(self, job_id: str) -> None:
        payload = self.store.legacy_record(job_id)
        if payload:
            _atomic_json(self._state_path(job_id), payload)

    @staticmethod
    def _expiry(seconds: int, *, start: str | None = None) -> str:
        base = datetime.fromisoformat(start) if start else datetime.now(timezone.utc)
        return (base + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _job_policy_ttl(
        record: Mapping[str, Any], key: str, fallback: int
    ) -> int:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return fallback

    @staticmethod
    def _recovery_directory_is_absent(path: Path) -> bool:
        """Return True only when a recovery root has no directory entry."""
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _recovery_outputs_are_absent(self, job_id: str) -> bool:
        # Existing empty directories are treated as unsafe/partial state.  The
        # adapter's fresh-output contract requires both per-job paths to be
        # absent before a queued task can be replayed.
        return self._recovery_directory_is_absent(self.config.output_root / job_id) and (
            self._recovery_directory_is_absent(self.config.staging_root / job_id)
        )

    def _inspect_recovery_artifacts(
        self,
        job_id: str,
        *,
        expected_input_sha256: str | None,
    ) -> tuple[Path | None, list[dict[str, Any]] | None, bool]:
        """Classify output/staging roots without mutating them.

        A complete root can be promoted only when no sibling root contains a
        partial or unsafe artifact.  This avoids treating a complete staging
        tree as authoritative while silently discarding a conflicting output
        tree left by an interrupted prior conversion.
        """
        output = self.config.output_root / job_id
        staging = self.config.staging_root / job_id
        complete: list[tuple[Path, list[dict[str, Any]]]] = []
        partial = False
        for root in (output, staging):
            if self._recovery_directory_is_absent(root):
                continue
            if expected_input_sha256 is None:
                partial = True
                continue
            try:
                manifest = self._validate_success_outputs(
                    root,
                    expected_input_sha256=expected_input_sha256,
                )
            except (OSError, ValueError):
                partial = True
            else:
                complete.append((root, manifest))

        if complete and not partial:
            # Preserve the historical output-first preference when both roots
            # happen to contain independently valid generations.
            for root, manifest in complete:
                if root == output:
                    return root, manifest, False
            return complete[0][0], complete[0][1], False
        return None, None, partial

    def _queued_job_is_replayable(self, record: Mapping[str, Any]) -> bool:
        """Validate the durable input and clean output boundary for replay."""
        job_id = str(record.get("job_id") or "")
        expected_input_sha256 = _normalize_hex_sha256(record.get("input_sha256"))
        if not job_id or expected_input_sha256 is None:
            return False
        # Once recovery has durably classified a queued row as unsafe, later
        # cleanup must never make the now-empty roots look replayable.  This
        # marker is committed before any partial publication is moved or
        # removed and survives a process crash or transient finalization error.
        if record.get("progress_stage") == _RECOVERY_INTERRUPTING_STAGE:
            return False
        if not self._recovery_outputs_are_absent(job_id):
            return False

        raw_input_path = record.get("input_path")
        if not isinstance(raw_input_path, str) or not raw_input_path:
            return False
        input_path = Path(raw_input_path)
        expected_input_path = self.config.input_root / job_id / "source.pdf"
        try:
            # Do not allow a queued record to replay another job's input (even
            # when that file happens to have the same digest).  Require the
            # exact canonical lexical path emitted by submit_job, then run the
            # symlink-aware resolver over every parent component.
            if input_path.absolute() != expected_input_path:
                return False
            safe_resolve(self.config.input_root, expected_input_path)
            if input_path.is_symlink() or not input_path.is_file():
                return False
            return _sha256_nofollow_path(input_path) == expected_input_sha256
        except (OSError, ValueError):
            return False

    def _finalize_recovered_interrupted(self, record: Mapping[str, Any]) -> None:
        job_id = str(record["job_id"])
        current_state = str(record.get("state") or "")
        if current_state not in {"queued", "running"}:
            raise RuntimeError("recovery interruption requires a nonterminal job")
        marker = self.store.update_job(
            job_id,
            state=current_state,
            progress_stage=_RECOVERY_INTERRUPTING_STAGE,
            progress_message="restart recovery classified artifacts as unsafe",
        )
        if marker.get("job_id") != job_id:
            raise RuntimeError(
                f"could not persist recovery interruption marker: {marker.get('error') or 'unknown_error'}"
            )
        manifest = self._publish_partial(job_id)
        updated = self.store.update_job(
            job_id,
            output_expires_at=self._expiry(
                self._job_policy_ttl(
                    record,
                    "failed_output_ttl_seconds",
                    self.config.failed_output_ttl_seconds,
                )
            ),
            tombstone_expires_at=self._expiry(
                self._job_policy_ttl(
                    record,
                    "job_ttl_seconds",
                    self.config.job_ttl_seconds,
                )
            ),
        )
        if updated.get("job_id") != job_id:
            raise RuntimeError(
                f"could not persist recovery retention policy: {updated.get('error') or 'unknown_error'}"
            )
        finalized = self.store.finalize_job(
            job_id,
            state="interrupted",
            manifest=manifest,
            error="service restarted before the job reached a terminal state",
            webhook_event_type="docling.job.interrupted",
            webhook_payload=self._webhook_payload(record, "interrupted"),
        )
        if finalized.get("error"):
            raise RuntimeError(
                f"could not finalize recovered interruption: {finalized['error']}"
            )

    def _finalize_recovered_success(
        self,
        record: Mapping[str, Any],
        source: Path,
        manifest: list[dict[str, Any]],
    ) -> None:
        job_id = str(record["job_id"])
        if source == self.config.staging_root / job_id:
            self._publish_staging(job_id)
        updated = self.store.update_job(
            job_id,
            output_expires_at=self._expiry(
                self._job_policy_ttl(
                    record,
                    "success_output_ttl_seconds",
                    self.config.success_output_ttl_seconds,
                )
            ),
            tombstone_expires_at=self._expiry(
                self._job_policy_ttl(
                    record,
                    "job_ttl_seconds",
                    self.config.job_ttl_seconds,
                )
            ),
        )
        if updated.get("job_id") != job_id:
            raise RuntimeError(
                f"could not persist recovered success policy: {updated.get('error') or 'unknown_error'}"
            )
        finalized = self.store.finalize_job(
            job_id,
            state="succeeded",
            manifest=manifest,
            exit_code=0,
            error=None,
            webhook_event_type="docling.job.succeeded",
            webhook_payload=self._webhook_payload(record, "succeeded"),
        )
        if finalized.get("error"):
            raise RuntimeError(
                f"could not finalize recovered success: {finalized['error']}"
            )

    def _recover_interrupted_jobs(self) -> None:
        for state in ("queued", "running"):
            cursor: str | None = None
            while True:
                page = self.store.list_jobs(
                    state=state,
                    cursor=cursor,
                    limit=1000,
                    include_tombstoned=True,
                )
                for record in page.get("items", []):
                    job_id = str(record["job_id"])
                    expected_input_sha256 = _normalize_hex_sha256(
                        record.get("input_sha256")
                    )
                    source, manifest, partial = self._inspect_recovery_artifacts(
                        job_id,
                        expected_input_sha256=expected_input_sha256,
                    )

                    # A queued job that never started is safe to replay only when
                    # both output roots are absent and its persisted
                    # source snapshot still matches the recorded digest.  Leave
                    # it queued until dependencies are healthy; no async adapter
                    # request is replayed during constructor recovery.
                    if (
                        state == "queued"
                        and source is None
                        and not partial
                        and self._queued_job_is_replayable(record)
                    ):
                        self._resume_pending_ids.add(job_id)
                        try:
                            self._mirror_job(job_id)
                        except Exception:
                            # Keep the queued marker for the health-gated
                            # worker even when the compatibility mirror is
                            # temporarily unavailable during startup.
                            LOGGER.warning(
                                "legacy mirror write failed during queued recovery for job %s",
                                job_id,
                                exc_info=True,
                            )
                        continue

                    try:
                        if source is None or manifest is None or partial:
                            raise ValueError(
                                "no complete recovery artifact is available"
                            )
                        self._finalize_recovered_success(record, source, manifest)
                    except (OSError, ValueError):
                        self._finalize_recovered_interrupted(record)
                    try:
                        self._mirror_job(job_id)
                    except Exception:
                        # SQLite finalization remains authoritative; retry the
                        # mirror from the same daemon worker rather than
                        # aborting startup or leaving a stale legacy state.
                        LOGGER.warning(
                            "legacy mirror write failed during terminal recovery for job %s",
                            job_id,
                            exc_info=True,
                        )
                        with self._lock:
                            self._resume_pending_ids.add(job_id)
                            self._resume_wake.set()
                cursor = page.get("next_cursor")
                if not cursor:
                    break

    def submit_job(
        self,
        input_path: Path,
        original_name: str,
        *,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        client_reference: str | None = None,
    ) -> tuple[JobRecord, bool]:
        # Runtime-config PATCH uses the same lock. A job therefore captures one
        # coherent lifecycle policy, and completion can never mix old/new TTLs.
        with self._lock:
            self._refresh_runtime_config_locked()
            lifecycle_policy = {
                "runtime_config_revision": self._runtime_config_revision,
                "input_ttl_seconds": self.config.input_ttl_seconds,
                "success_output_ttl_seconds": self.config.success_output_ttl_seconds,
                "failed_output_ttl_seconds": self.config.failed_output_ttl_seconds,
                "job_ttl_seconds": self.config.job_ttl_seconds,
                "idempotency_ttl_seconds": self.config.idempotency_ttl_seconds,
            }
        input_size = input_path.stat().st_size
        input_sha256 = _sha256_path(input_path)
        fingerprint = request_fingerprint or hashlib.sha256(
            (
                input_sha256
                + "\0"
                + original_name
                + "\0"
                + str(input_size)
                + "\0"
                + (client_reference or "")
            ).encode("utf-8")
        ).hexdigest()
        if idempotency_key:
            existing_key = self.store.resolve_idempotency_key(idempotency_key)
            if existing_key:
                if existing_key.get("request_fingerprint") != fingerprint:
                    raise FileExistsError("idempotency_conflict")
                existing_job = self.store.get_job(str(existing_key["job_id"]))
                if existing_job:
                    return self._record_from_mapping(existing_job), True
        self._quota.check(
            self.store,
            input_bytes=input_size,
            data_root=self.config.output_root,
            expected_output_bytes=self.config.max_output_bytes,
        )
        if (
            shutil.disk_usage(self.config.input_root).free
            < self.config.min_free_bytes + input_size
        ):
            raise StorageQuotaError("input filesystem would fall below min_free_bytes")
        job_id = str(uuid.uuid4())
        final_input = self.config.input_root / job_id / "source.pdf"
        self._mark_pending_input(job_id)
        try:
            final_input.parent.mkdir(parents=True, exist_ok=False)
            _move_input_file(input_path, final_input)
            if shutil.disk_usage(self.config.input_root).free < self.config.min_free_bytes:
                raise StorageQuotaError(
                    "input filesystem crossed min_free_bytes while accepting upload"
                )
            created_at = utc_now()
            result = self.store.create_job_with_idempotency(
                idempotency_key=idempotency_key or f"internal:{job_id}",
                job_id=job_id,
                request_fingerprint=fingerprint,
                idempotency_ttl_seconds=lifecycle_policy["idempotency_ttl_seconds"],
                original_name=original_name,
                client_reference=client_reference,
                input_path=str(final_input),
                output_dir=str(self.config.output_root / job_id),
                input_sha256=input_sha256,
                input_size_bytes=input_size,
                reserved_output_bytes=self.config.max_output_bytes,
                input_expires_at=self._expiry(
                    lifecycle_policy["input_ttl_seconds"], start=created_at
                ),
                runtime_config_revision=lifecycle_policy["runtime_config_revision"],
                input_ttl_seconds=lifecycle_policy["input_ttl_seconds"],
                success_output_ttl_seconds=lifecycle_policy[
                    "success_output_ttl_seconds"
                ],
                failed_output_ttl_seconds=lifecycle_policy[
                    "failed_output_ttl_seconds"
                ],
                job_ttl_seconds=lifecycle_policy["job_ttl_seconds"],
                created_at=created_at,
            )
        except BaseException:
            if final_input.parent.exists():
                safe_delete_tree(self.config.input_root, final_input.parent)
            raise
        finally:
            self._clear_pending_input(job_id)
        if result.get("error"):
            if final_input.parent.exists():
                safe_delete_tree(self.config.input_root, final_input.parent)
            error = str(result["error"])
            if error == "idempotency_conflict":
                raise FileExistsError(error)
            if error == "queue_full":
                from .lifecycle import QueueFullError

                raise QueueFullError(error)
            if error == "quota_exceeded":
                from .lifecycle import StorageQuotaError

                raise StorageQuotaError(error)
            raise RuntimeError(error)
        replayed = bool(result.pop("_idempotent_replay", False))
        if replayed:
            safe_delete_tree(self.config.input_root, final_input.parent)
        record = self._record_from_mapping(result)
        try:
            self._mirror_job(record.job_id)
        except Exception:
            # The SQLite row is already durable and remains the source of
            # truth.  Do not turn a successful enqueue into an API 500 just
            # because the rollback mirror is temporarily unwritable; the
            # worker/recovery path will retry terminal mirroring.
            LOGGER.warning(
                "legacy mirror write failed after enqueue for job %s",
                record.job_id,
                exc_info=True,
            )
        if replayed:
            return record, True
        try:
            submitted = self._submit_job_once(job_id)
        except Exception:
            # The durable row remains queued when executor submission fails.
            # Treat dispatch as an internal delayed operation: the accepted
            # request still returns its stable job id, while the same
            # health-gated worker (or the next process restart) retries it.
            with self._lock:
                self._resume_pending_ids.add(job_id)
            self._resume_wake.set()
            self._start_queued_resume_worker()
            return record, False
        if not submitted:
            with self._lock:
                current = self.store.get_job(job_id)
                if current and current.get("state") == "queued":
                    self._resume_pending_ids.add(job_id)
            self._resume_wake.set()
            self._start_queued_resume_worker()
        return record, False

    def create_job(self, input_path: Path, original_name: str) -> JobRecord:
        record, _replayed = self.submit_job(input_path, original_name)
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        try:
            uuid.UUID(job_id)
        except ValueError:
            return None
        record = self.store.get_job(job_id)
        return self._record_from_mapping(record) if record else None

    def get_job_details(self, job_id: str) -> dict[str, Any]:
        try:
            uuid.UUID(job_id)
        except ValueError:
            return {}
        return self.store.get_job(job_id)

    @staticmethod
    def _webhook_payload(record: Mapping[str, Any], state: str) -> dict[str, Any]:
        job_id = str(record["job_id"])
        return {
            "job_id": job_id,
            "state": state,
            "original_name": record.get("original_name"),
            "client_reference": record.get("client_reference"),
            "status_url": f"/v1/jobs/{job_id}",
            "outputs_url": f"/v1/jobs/{job_id}/outputs",
            "manifest_url": f"/v1/jobs/{job_id}/manifest",
            "archive_url": f"/v1/jobs/{job_id}/archive",
        }

    def _collect_manifest(self, root: Path) -> list[dict[str, Any]]:
        if root.is_symlink():
            raise ValueError("output job directory cannot be a symlink")
        if not root.is_dir():
            return []
        resolved_root = root.resolve()
        files: list[dict[str, Any]] = []
        total = 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"output contains a symlink: {path.name}")
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved_root not in resolved.parents:
                raise ValueError("output path escapes the job directory")
            size = path.stat().st_size
            total += size
            if total > self.config.max_output_bytes:
                raise ValueError("job output exceeds DOCLING_MAX_OUTPUT_BYTES")
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": size,
                    "sha256": _sha256_path(path),
                    "media_type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                }
            )
        return files

    def _validate_success_outputs(
        self, root: Path, *, expected_input_sha256: str | None = None
    ) -> list[dict[str, Any]]:
        if root.is_symlink():
            raise ValueError("staging job directory cannot be a symlink")
        if not root.is_dir():
            raise ValueError("conversion did not produce an output directory")
        for name in REQUIRED_SUCCESS_OUTPUTS:
            candidate = root / name
            if not candidate.is_file() or candidate.is_symlink():
                raise ValueError(f"required output is missing: {name}")
        try:
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("status.json is invalid") from exc
        if not isinstance(status, dict) or status.get("ok") is not True:
            raise ValueError("status.json did not report ok=true")
        if expected_input_sha256 is not None:
            normalized_expected_input_sha256 = _normalize_hex_sha256(
                expected_input_sha256
            )
            if normalized_expected_input_sha256 is None:
                raise ValueError("expected_input_sha256 must be a 64-character hex digest")
            metadata_path = root / "metadata.json"
            if metadata_path.is_symlink() or not metadata_path.is_file():
                raise ValueError("metadata.json is not a regular file")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("metadata.json is invalid") from exc
            if not isinstance(metadata, Mapping):
                raise ValueError("metadata.json is invalid")
            if (
                str(metadata.get("original_input_sha256") or "").lower()
                != normalized_expected_input_sha256
            ):
                raise ValueError(
                    "metadata.original_input_sha256 does not match expected input"
                )
            if (
                str(metadata.get("visual_evidence_input_sha256") or "").lower()
                != normalized_expected_input_sha256
            ):
                raise ValueError(
                    "metadata.visual_evidence_input_sha256 does not match expected input"
                )
            try:
                source_pdf_path = root / "source.pdf"
                source_pdf_sha256 = _sha256_nofollow_path(source_pdf_path)
                if source_pdf_sha256 != normalized_expected_input_sha256:
                    raise ValueError("source.pdf does not match expected input")
            except FileNotFoundError as exc:
                raise ValueError("source.pdf is missing") from exc
            except ValueError as exc:
                raise
            except OSError as exc:
                if getattr(os, "O_NOFOLLOW", None) is not None and exc.errno == errno.ELOOP:
                    raise ValueError("source.pdf is not a regular file") from exc
                raise ValueError("source.pdf is not readable") from exc
        return self._collect_manifest(root)

    def _publish_staging(self, job_id: str) -> Path:
        staging = self.config.staging_root / job_id
        target = self.config.output_root / job_id
        if staging.is_symlink() or target.is_symlink():
            raise ValueError("staging and output job directories cannot be symlinks")
        if target.exists():
            # A fresh conversion must never discard a validated staging tree
            # in favor of an already-existing target.  Treat any target entry
            # (including an empty directory created during a TOCTOU window)
            # as a publication conflict and let the caller fail closed.
            raise ValueError("output job directory already exists")
        if not staging.is_dir():
            raise ValueError("staging output is missing")
        os.replace(staging, target)
        return target

    def _publish_partial(self, job_id: str) -> list[dict[str, Any]]:
        staging = self.config.staging_root / job_id
        target = self.config.output_root / job_id
        try:
            if staging.is_symlink():
                staging.unlink(missing_ok=True)
                return []
            if target.is_symlink():
                target.unlink(missing_ok=True)
                if staging.exists():
                    safe_delete_tree(self.config.staging_root, staging)
                return []
            if target.exists():
                # The final target was not published by this invocation.  It
                # may belong to a concurrent or hostile writer, so never
                # advertise its files as this job's failed output.  Preserve
                # our staging tree for bounded retention/forensics rather
                # than overwriting or deleting either tree.
                return []
            if staging.is_dir() and not target.exists():
                os.replace(staging, target)
            return self._collect_manifest(target)
        except (OSError, ValueError):
            if staging.exists():
                safe_delete_tree(self.config.staging_root, staging)
            if target.exists():
                safe_delete_tree(self.config.output_root, target)
            return []

    @staticmethod
    def _tree_size_until(root: Path, limit: int) -> int:
        """Return a cheap staging size estimate, stopping once limit is crossed."""
        if not root.is_dir():
            return 0
        total = 0

        def raise_walk_error(error: OSError) -> None:
            raise error

        for directory, _subdirectories, filenames in os.walk(
            root, followlinks=False, onerror=raise_walk_error
        ):
            for filename in filenames:
                path = Path(directory) / filename
                try:
                    if path.is_symlink():
                        continue
                    total += path.stat().st_size
                except FileNotFoundError:
                    continue
                if total > limit:
                    return total
        return total

    @staticmethod
    def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - release profiles are macOS/Linux
                process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait()

    def _run_production_adapter(
        self, command: list[str], *, job_id: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the real adapter while bounding logs, time, and staging growth."""
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        max_log_bytes = 4 * 1024 * 1024
        stdout_tail = bytearray()
        stderr_tail = bytearray()

        def drain(stream: Any, target: bytearray) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    target.extend(chunk)
                    overflow = len(target) - max_log_bytes
                    if overflow > 0:
                        del target[:overflow]
            finally:
                stream.close()

        threads = [
            threading.Thread(target=drain, args=(process.stdout, stdout_tail), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr_tail), daemon=True),
        ]
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + self.config.conversion_timeout_seconds + 120
        staging = self.config.staging_root / job_id
        failure: Exception | None = None
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    failure = subprocess.TimeoutExpired(
                        command, self.config.conversion_timeout_seconds + 120
                    )
                    break
                if (
                    self._tree_size_until(staging, self.config.max_output_bytes)
                    > self.config.max_output_bytes
                ):
                    failure = ValueError(
                        "job output exceeded DOCLING_MAX_OUTPUT_BYTES while converting"
                    )
                    break
                if shutil.disk_usage(self.config.output_root).free < self.config.min_free_bytes:
                    failure = ValueError(
                        "output filesystem crossed DOCLING_MIN_FREE_BYTES while converting"
                    )
                    break
                time.sleep(0.2)
        except OSError as exc:
            failure = ValueError(f"could not monitor staging output: {exc}")
        except BaseException:
            self._stop_process_group(process)
            raise
        finally:
            if failure is not None:
                self._stop_process_group(process)
            for thread in threads:
                thread.join(timeout=10)
        if failure is not None:
            raise failure
        return subprocess.CompletedProcess(
            command,
            int(process.returncode),
            stdout_tail.decode("utf-8", errors="replace"),
            stderr_tail.decode("utf-8", errors="replace"),
        )

    def _run(self, job_id: str) -> None:
        with self._lock:
            details = self.store.get_job(job_id)
            if not details:
                return
            updated = self.store.update_job(
                job_id,
                state="running",
                started_at=utc_now(),
                error=None,
                progress_stage="converting",
                progress_message="conversion adapter running",
            )
            if updated.get("error"):
                return
            try:
                self._mirror_job(job_id)
            except Exception:
                # SQLite is authoritative.  A transient legacy-mirror write
                # failure must not strand a durable running job before the
                # adapter has even started; terminal mirroring below (and its
                # tracked retry) will converge the compatibility file.
                LOGGER.warning(
                    "legacy mirror write failed at conversion start for job %s",
                    job_id,
                    exc_info=True,
                )
        record = self._record_from_mapping(updated)
        state = "failed"
        exit_code: int | None = None
        error: str | None = None
        manifest: list[dict[str, Any]] = []
        try:
            command = build_adapter_command(
                self.config,
                record,
                output_root=self.config.staging_root,
            )
            expected_input_sha256 = _normalize_hex_sha256(record.input_sha256)
            if expected_input_sha256 is None:
                raise ValueError("missing or invalid input_sha256 on production job record")
            if not self._recovery_outputs_are_absent(job_id):
                raise ValueError(
                    "output job directory already exists before conversion"
                )
            if self._runner is subprocess.run:
                completed = self._run_production_adapter(command, job_id=job_id)
            else:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.config.conversion_timeout_seconds + 120,
                    check=False,
                )
            exit_code = completed.returncode
            if completed.returncode == 0:
                self.store.update_progress(
                    job_id,
                    "validating",
                    message="checking the published output contract",
                )
                manifest = self._validate_success_outputs(
                    self.config.staging_root / job_id,
                    expected_input_sha256=expected_input_sha256,
                )
                self.store.update_progress(
                    job_id,
                    "publishing",
                    message="publishing validated artifacts",
                )
                self._publish_staging(job_id)
                state = "succeeded"
            else:
                detail = (completed.stderr or completed.stdout or "conversion failed").strip()
                error = detail[-4000:]
                manifest = self._publish_partial(job_id)
        except subprocess.TimeoutExpired:
            error = "conversion process exceeded its release timeout"
            manifest = self._publish_partial(job_id)
        except OSError as exc:
            error = f"could not start quality adapter: {exc}"
            manifest = self._publish_partial(job_id)
        except ValueError as exc:
            error = str(exc)
            manifest = self._publish_partial(job_id)
        except Exception as exc:
            # Executor futures are not otherwise observed. Always converge the
            # durable task to a terminal state on unexpected adapter failures.
            error = f"unexpected conversion failure: {type(exc).__name__}: {exc}"
            manifest = self._publish_partial(job_id)
        output_ttl = self._job_policy_ttl(
            updated,
            (
                "success_output_ttl_seconds"
                if state == "succeeded"
                else "failed_output_ttl_seconds"
            ),
            (
                self.config.success_output_ttl_seconds
                if state == "succeeded"
                else self.config.failed_output_ttl_seconds
            ),
        )
        tombstone_ttl = self._job_policy_ttl(
            updated,
            "job_ttl_seconds",
            self.config.job_ttl_seconds,
        )
        event_type = f"docling.job.{state}"
        with self._lock:
            self.store.update_job(
                job_id,
                output_expires_at=self._expiry(output_ttl),
                tombstone_expires_at=self._expiry(tombstone_ttl),
            )
            result = self.store.finalize_job(
                job_id,
                state=state,
                manifest=manifest,
                exit_code=exit_code,
                error=error,
                webhook_event_type=event_type,
                webhook_payload=self._webhook_payload(updated, state),
            )
            if result.get("error"):
                return
            self._mirror_job(job_id)

    def output_files(self, job_id: str) -> list[dict[str, Any]]:
        record = self.store.get_job(job_id)
        if not record:
            raise FileNotFoundError(job_id)
        if record.get("output_deleted_at"):
            return []
        output_expires_at = record.get("output_expires_at")
        if isinstance(output_expires_at, str) and output_expires_at:
            try:
                deadline = datetime.fromisoformat(output_expires_at.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline <= datetime.now(timezone.utc):
                    raise OutputExpiredError("output_expired")
            except ValueError:
                pass
        manifest = self.store.list_manifest(job_id)
        files = []
        for item in manifest.get("items", []):
            relative = str(item["path"])
            files.append(
                {
                    "path": relative,
                    "size_bytes": int(item["size_bytes"]),
                    "sha256": str(item["sha256"]),
                    "media_type": item.get("media_type")
                    or "application/octet-stream",
                    "download_url": (
                        f"/v1/jobs/{job_id}/files/"
                        f"{urllib.parse.quote(relative, safe='/')}"
                    ),
                    "expires_at": record.get("output_expires_at"),
                }
            )
        return files

    def resolve_output_root(self, job_id: str) -> Path:
        record = self.store.get_job(job_id)
        if not record or record.get("output_deleted_at"):
            raise FileNotFoundError(job_id)
        raw_root = Path(str(record["output_dir"]))
        expected_root = self.config.output_root / job_id
        if raw_root != expected_root or raw_root.absolute().is_symlink():
            raise PermissionError("output job directory is outside its configured boundary")
        root = safe_resolve(self.config.output_root, raw_root)
        if root != expected_root.resolve() or not root.is_dir():
            raise PermissionError("output job directory is outside its configured boundary")
        return root

    def _published_output_item(self, job_id: str, relative_path: str) -> dict[str, Any]:
        published = {
            str(item.get("path")): item
            for item in self.store.list_manifest(job_id).get("items", [])
        }
        manifest_item = published.get(relative_path)
        if manifest_item is None:
            raise FileNotFoundError(relative_path)
        return manifest_item

    def open_output_file(self, job_id: str, relative_path: str) -> tuple[Any, int]:
        """Return a verified, already-open output handle anchored below the job root."""

        root = self.resolve_output_root(job_id)
        manifest_item = self._published_output_item(job_id, relative_path)
        try:
            handle = open_relative_file(root, relative_path)
        except OSError as exc:
            raise FileNotFoundError(relative_path) from exc
        try:
            size_bytes = int(os.fstat(handle.fileno()).st_size)
            if size_bytes != int(manifest_item.get("size_bytes", -1)):
                raise ValueError("output size no longer matches the published manifest")
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            expected_sha256 = str(manifest_item.get("sha256") or "")
            if len(expected_sha256) != 64 or digest.hexdigest() != expected_sha256:
                raise ValueError("output hash no longer matches the published manifest")
            handle.seek(0)
            return handle, size_bytes
        except BaseException:
            handle.close()
            raise

    def resolve_output_file(self, job_id: str, relative_path: str) -> Path:
        root = self.resolve_output_root(job_id)
        candidate = safe_resolve(root, root / relative_path)
        if candidate == root:
            raise PermissionError("output path must name a file")
        manifest_item = self._published_output_item(job_id, relative_path)
        if not candidate.is_file() or candidate.is_symlink():
            raise FileNotFoundError(relative_path)
        if candidate.stat().st_size != int(manifest_item.get("size_bytes", -1)):
            raise ValueError("output size no longer matches the published manifest")
        expected_sha256 = str(manifest_item.get("sha256") or "")
        if len(expected_sha256) != 64 or _sha256_path(candidate) != expected_sha256:
            raise ValueError("output hash no longer matches the published manifest")
        return candidate

    def list_jobs(
        self,
        *,
        state: str | None = None,
        client_reference: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self.store.list_jobs(
            state=state,
            client_reference=client_reference,
            cursor=cursor,
            limit=min(max(limit, 1), 100),
        )

    def manifest(self, job_id: str) -> dict[str, Any]:
        record = self.store.get_job(job_id)
        if not record:
            raise FileNotFoundError(job_id)
        files = self.output_files(job_id)
        return {
            "job_id": job_id,
            "manifest_sha256": record.get("manifest_sha256"),
            "files": files,
            "expires_at": record.get("output_expires_at"),
        }

    def acquire_download_lease(
        self, job_id: str, relative_path: str, *, holder: str | None = None
    ) -> DownloadLease:
        result = self.store.acquire_download_lease(
            job_id,
            relative_path,
            holder=holder or str(uuid.uuid4()),
            ttl_seconds=self.config.download_lease_seconds,
        )
        if result.get("error"):
            if result["error"] == "invalid_relative_path":
                raise PermissionError(str(result["error"]))
            if result["error"] == "output_expired":
                raise OutputExpiredError(str(result["error"]))
            if result["error"] in {"job_not_found", "file_not_published"}:
                raise FileNotFoundError(str(result["error"]))
            raise RuntimeError(str(result["error"]))
        return DownloadLease(
            self.store,
            str(result["lease_id"]),
            self.config.download_lease_seconds,
        )

    def storage_status(self) -> dict[str, Any]:
        stats = self.store.pending_and_bytes_stats()
        input_bytes = int(stats.get("input_bytes", 0))
        output_bytes = int(stats.get("output_bytes", 0))
        reserved = int(stats.get("reserved_output_bytes", 0))
        return {
            "usage": {
                "pending_jobs": int(stats.get("pending_jobs", 0)),
                "input_bytes": input_bytes,
                "output_bytes": output_bytes,
                "reserved_output_bytes": reserved,
                "total_managed_bytes": input_bytes + output_bytes + reserved,
                "filesystem_free_bytes": shutil.disk_usage(
                    self.config.output_root
                ).free,
            },
            "limits": {
                "max_pending_jobs": self.config.max_pending_jobs,
                "max_data_bytes": self.config.max_data_bytes,
                "max_output_bytes": self.config.max_output_bytes,
                "min_free_bytes": self.config.min_free_bytes,
            },
            "cleanup_interval_seconds": self.config.cleanup_interval_seconds,
        }

    def delete_job(self, job_id: str) -> bool:
        record = self.store.get_job(job_id)
        if not record:
            raise FileNotFoundError(job_id)
        if record.get("state") not in TERMINAL_STATES:
            raise RuntimeError("job_not_terminal")
        now = datetime.now(timezone.utc).timestamp()

        # Claim output first. The store checks download leases and publishes
        # the cleanup claim in one transaction; later lease acquisition checks
        # that same claim, so neither side can slip through the other's check.
        for kind, root, target, deleted_field in (
            ("output", self.config.output_root, Path(str(record["output_dir"])), "output_deleted_at"),
            ("input", self.config.input_root, Path(str(record["input_path"])).parent, "input_deleted_at"),
        ):
            current = self.store.get_job(job_id)
            if current.get(deleted_field):
                continue
            lease_id = self.store.claim_cleanup(job_id, kind, now)
            if not lease_id:
                current = self.store.get_job(job_id)
                if current.get(deleted_field):
                    continue
                if kind == "output" and self.store.has_active_download(job_id):
                    raise RuntimeError("download_in_progress")
                raise RuntimeError(f"{kind}_cleanup_in_progress")
            try:
                deleted = safe_delete_tree(root, target)
            except Exception as exc:
                self.store.complete_cleanup(
                    job_id,
                    kind,
                    lease_id=lease_id,
                    deleted_bytes=0,
                    error=str(exc),
                )
                raise
            self.store.complete_cleanup(
                job_id,
                kind,
                lease_id=lease_id,
                deleted_bytes=deleted,
                error=None,
            )
        deleted_at = utc_now()
        tombstone_expires_at = record.get("tombstone_expires_at") or self._expiry(
            self._job_policy_ttl(
                record,
                "job_ttl_seconds",
                self.config.job_ttl_seconds,
            )
        )
        self.store.update_job(
            job_id,
            tombstone_expires_at=tombstone_expires_at,
            delete_requested_at=deleted_at,
            deleted=True,
            deleted_at=deleted_at,
        )
        self.store.tombstone_jobs([job_id], reason="user_deleted")
        self._mirror_job(job_id)
        return True

    def validate_webhook_url(self, callback_url: str) -> None:
        private_hosts = (
            set(self.config.webhook_allowed_hosts)
            if self.config.webhook_allow_private_hosts
            else set()
        )
        validate_callback_url(
            callback_url,
            allowed_hosts=set(self.config.webhook_allowed_hosts),
            allow_private_hosts=private_hosts,
        )

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
        self._resume_stop.set()
        self._resume_wake.set()
        if self._resume_thread is not None:
            self._resume_thread.join(timeout=None)
        self._janitor.stop(wait=None)
        if self._dispatcher is not None:
            self._dispatcher.close()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.store.close()
