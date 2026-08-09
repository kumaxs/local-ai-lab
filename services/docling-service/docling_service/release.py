"""Shared release runtime for the macOS and Docker service profiles."""

from __future__ import annotations

import errno
import hashlib
import json
import mimetypes
import os
import platform
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contract import REQUIRED_SUCCESS_OUTPUTS
from .lifecycle import (
    Janitor,
    QuotaManager,
    QuotaPolicy,
    RetentionPolicy,
    safe_delete_tree,
    safe_resolve,
)
from .persistence import SQLiteStore
from .webhook import WebhookDispatcher, validate_callback_url


RELEASE_VERSION = "1.1.0"
TERMINAL_STATES = {"succeeded", "failed", "interrupted"}


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
        if formula_ocr_url is None and profile == "docker" and formula_policy == "formula_service":
            formula_ocr_url = "http://formula:8001"
        timeout_default = "7200" if profile == "docker" else "3600"
        return cls(
            profile=profile,
            serve_url=os.getenv("DOCLING_SERVE_URL", "http://127.0.0.1:5001").rstrip("/"),
            adapter_path=Path(os.getenv("DOCLING_QUALITY_ADAPTER", str(default_adapter))).resolve(),
            input_root=Path(os.getenv("DOCLING_INPUT_ROOT", str(default_data / "inputs"))).resolve(),
            output_root=Path(os.getenv("DOCLING_OUTPUT_ROOT", str(default_data / "outputs"))).resolve(),
            state_root=Path(os.getenv("DOCLING_STATE_ROOT", str(default_data / "state"))).resolve(),
            max_upload_bytes=int(os.getenv("DOCLING_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024))),
            max_concurrent_jobs=max(1, int(os.getenv("DOCLING_MAX_CONCURRENT_JOBS", "1"))),
            conversion_timeout_seconds=max(
                60,
                int(os.getenv("DOCLING_CONVERSION_TIMEOUT_SECONDS", timeout_default)),
            ),
            image_export_mode=os.getenv("DOCLING_IMAGE_EXPORT_MODE", "embedded"),
            formula_policy=formula_policy,
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
            path.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.state_root / "control.sqlite3"

    @property
    def staging_root(self) -> Path:
        return self.output_root / ".staging"

    @property
    def temp_root(self) -> Path:
        return self.state_root / "temp"

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
        "apply-all",
    ]
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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        self._pending_input_ids: set[str] = set()
        self._active_temp_names: set[str] = set()
        self.store.import_legacy_state_jobs(config.state_root)
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
                input_ttl=config.input_ttl_seconds,
                success_output_ttl=config.success_output_ttl_seconds,
                failed_output_ttl=config.failed_output_ttl_seconds,
                tombstone_ttl=config.job_ttl_seconds,
                staging_ttl=config.staging_ttl_seconds,
                temp_ttl=config.temp_ttl_seconds,
            ),
            input_root=config.input_root,
            output_root=config.output_root,
            tombstone_root=config.state_root / "jobs",
            staging_root=config.staging_root,
            temp_root=config.temp_root,
            download_lease=self.store.has_active_download,
            scan_interval_seconds=config.cleanup_interval_seconds,
            pending_inputs=self._pending_inputs_snapshot,
            protected_temp_entries=self._active_temp_snapshot,
            maintenance=(
                self.store.purge_expired_download_leases,
                self.store.purge_expired_idempotency_keys,
                lambda: self.store.purge_webhook_deliveries(
                    max_age_seconds=config.webhook_delivery_ttl_seconds
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

    def _state_path(self, job_id: str) -> Path:
        return self.config.state_root / "jobs" / f"{job_id}.json"

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

    def _write_record(self, record: JobRecord) -> None:
        _atomic_json(self._state_path(record.job_id), asdict(record))

    @staticmethod
    def _record_from_mapping(record: Mapping[str, Any]) -> JobRecord:
        return JobRecord(
            **{field: record.get(field) for field in SQLiteStore.LEGACY_FIELDS}
        )

    def _mirror_job(self, job_id: str) -> None:
        payload = self.store.legacy_record(job_id)
        if payload:
            _atomic_json(self._state_path(job_id), payload)

    @staticmethod
    def _expiry(seconds: int, *, start: str | None = None) -> str:
        base = datetime.fromisoformat(start) if start else datetime.now(timezone.utc)
        return (base + timedelta(seconds=seconds)).isoformat()

    def _recover_interrupted_jobs(self) -> None:
        for state in ("queued", "running"):
            page = self.store.list_jobs(state=state, limit=1000, include_tombstoned=True)
            for record in page.get("items", []):
                job_id = str(record["job_id"])
                candidate = self.config.output_root / job_id
                staging = self.config.staging_root / job_id
                try:
                    source = candidate if candidate.is_dir() else staging
                    manifest = self._validate_success_outputs(source)
                    if source == staging:
                        self._publish_staging(job_id)
                    self.store.update_job(
                        job_id,
                        output_expires_at=self._expiry(
                            self.config.success_output_ttl_seconds
                        ),
                        tombstone_expires_at=self._expiry(self.config.job_ttl_seconds),
                    )
                    self.store.finalize_job(
                        job_id,
                        state="succeeded",
                        manifest=manifest,
                        exit_code=0,
                        error=None,
                        webhook_event_type="docling.job.succeeded",
                        webhook_payload=self._webhook_payload(record, "succeeded"),
                    )
                except (OSError, ValueError):
                    manifest = self._publish_partial(job_id)
                    self.store.update_job(
                        job_id,
                        output_expires_at=self._expiry(
                            self.config.failed_output_ttl_seconds
                        ),
                        tombstone_expires_at=self._expiry(self.config.job_ttl_seconds),
                    )
                    self.store.finalize_job(
                        job_id,
                        state="interrupted",
                        manifest=manifest,
                        error="service restarted before the job reached a terminal state",
                        webhook_event_type="docling.job.interrupted",
                        webhook_payload=self._webhook_payload(record, "interrupted"),
                    )
                self._mirror_job(job_id)

    def submit_job(
        self,
        input_path: Path,
        original_name: str,
        *,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        client_reference: str | None = None,
    ) -> tuple[JobRecord, bool]:
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
                idempotency_ttl_seconds=self.config.idempotency_ttl_seconds,
                original_name=original_name,
                client_reference=client_reference,
                input_path=str(final_input),
                output_dir=str(self.config.output_root / job_id),
                input_sha256=input_sha256,
                input_size_bytes=input_size,
                reserved_output_bytes=self.config.max_output_bytes,
                input_expires_at=self._expiry(
                    self.config.input_ttl_seconds, start=created_at
                ),
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
        self._mirror_job(record.job_id)
        if replayed:
            return record, True
        self._executor.submit(self._run, job_id)
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

    def _validate_success_outputs(self, root: Path) -> list[dict[str, Any]]:
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
        return self._collect_manifest(root)

    def _publish_staging(self, job_id: str) -> Path:
        staging = self.config.staging_root / job_id
        target = self.config.output_root / job_id
        if staging.is_symlink() or target.is_symlink():
            raise ValueError("staging and output job directories cannot be symlinks")
        if target.exists():
            if staging.exists():
                safe_delete_tree(self.config.staging_root, staging)
            return target
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
            )
            if updated.get("error"):
                return
            self._mirror_job(job_id)
        record = self._record_from_mapping(updated)
        command = build_adapter_command(
            self.config,
            record,
            output_root=self.config.staging_root,
        )
        state = "failed"
        exit_code: int | None = None
        error: str | None = None
        manifest: list[dict[str, Any]] = []
        try:
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
                manifest = self._validate_success_outputs(
                    self.config.staging_root / job_id
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
        output_ttl = (
            self.config.success_output_ttl_seconds
            if state == "succeeded"
            else self.config.failed_output_ttl_seconds
        )
        event_type = f"docling.job.{state}"
        with self._lock:
            self.store.update_job(
                job_id,
                output_expires_at=self._expiry(output_ttl),
                tombstone_expires_at=self._expiry(self.config.job_ttl_seconds),
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

    def resolve_output_file(self, job_id: str, relative_path: str) -> Path:
        record = self.store.get_job(job_id)
        if not record:
            raise FileNotFoundError(job_id)
        if record.get("output_deleted_at"):
            raise FileNotFoundError(relative_path)
        raw_root = Path(str(record["output_dir"]))
        expected_root = self.config.output_root / job_id
        if raw_root != expected_root or raw_root.is_symlink():
            raise PermissionError("output job directory is outside its configured boundary")
        root = safe_resolve(self.config.output_root, raw_root)
        if root != expected_root.resolve():
            raise PermissionError("output job directory is outside its configured boundary")
        candidate = safe_resolve(root, root / relative_path)
        if candidate == root:
            raise PermissionError("output path must name a file")
        published = {
            str(item.get("path")): item
            for item in self.store.list_manifest(job_id).get("items", [])
        }
        manifest_item = published.get(relative_path)
        if manifest_item is None:
            raise FileNotFoundError(relative_path)
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
            if result["error"] in {
                "job_not_found",
                "output_expired",
                "file_not_published",
            }:
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
        if self.store.has_active_download(job_id):
            raise RuntimeError("download_in_progress")
        now = datetime.now(timezone.utc).timestamp()
        for kind, root, target in (
            ("input", self.config.input_root, Path(str(record["input_path"])).parent),
            ("output", self.config.output_root, Path(str(record["output_dir"]))),
        ):
            lease_id = self.store.claim_cleanup(job_id, kind, now)
            if not lease_id:
                continue
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
        self.store.update_job(
            job_id,
            tombstone_expires_at=self._expiry(self.config.job_ttl_seconds),
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
        self._janitor.stop(wait=None)
        if self._dispatcher is not None:
            self._dispatcher.close()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.store.close()
