"""Shared release runtime for the macOS and Docker service profiles."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


RELEASE_VERSION = "1.0.1"
TERMINAL_STATES = {"succeeded", "failed", "interrupted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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

    def ensure_directories(self) -> None:
        for path in (self.input_root, self.output_root, self.state_root):
            path.mkdir(parents=True, exist_ok=True)

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
            "max_concurrent_jobs": self.max_concurrent_jobs,
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


def build_adapter_command(config: ReleaseConfig, record: JobRecord) -> list[str]:
    command = [
        sys.executable,
        str(config.adapter_path),
        "--serve-url",
        config.serve_url,
        "--input-file",
        record.input_path,
        "--output-root",
        str(config.output_root),
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


class JobManager:
    """Bounded local conversion queue with restart-visible on-disk state."""

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
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_jobs,
            thread_name_prefix="docling-release",
        )
        self._recover_interrupted_jobs()

    def _state_path(self, job_id: str) -> Path:
        return self.config.state_root / "jobs" / f"{job_id}.json"

    def _write_record(self, record: JobRecord) -> None:
        _atomic_json(self._state_path(record.job_id), asdict(record))

    def _recover_interrupted_jobs(self) -> None:
        jobs_dir = self.config.state_root / "jobs"
        if not jobs_dir.exists():
            return
        for path in jobs_dir.glob("*.json"):
            try:
                record = JobRecord(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if record.state in {"queued", "running"}:
                record.state = "interrupted"
                record.finished_at = utc_now()
                record.error = "service restarted before the job reached a terminal state"
                self._write_record(record)

    def create_job(self, input_path: Path, original_name: str) -> JobRecord:
        job_id = str(uuid.uuid4())
        final_input = self.config.input_root / job_id / "source.pdf"
        final_input.parent.mkdir(parents=True, exist_ok=False)
        input_path.replace(final_input)
        record = JobRecord(
            job_id=job_id,
            state="queued",
            original_name=original_name,
            input_path=str(final_input),
            output_dir=str(self.config.output_root / job_id),
            created_at=utc_now(),
        )
        self._write_record(record)
        self._executor.submit(self._run, job_id)
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        try:
            uuid.UUID(job_id)
        except ValueError:
            return None
        path = self._state_path(job_id)
        if not path.is_file():
            return None
        try:
            return JobRecord(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _run(self, job_id: str) -> None:
        with self._lock:
            record = self.get_job(job_id)
            if record is None:
                return
            record.state = "running"
            record.started_at = utc_now()
            self._write_record(record)
        command = build_adapter_command(self.config, record)
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.conversion_timeout_seconds + 120,
                check=False,
            )
            record.exit_code = completed.returncode
            record.state = "succeeded" if completed.returncode == 0 else "failed"
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "conversion failed").strip()
                record.error = detail[-4000:]
        except subprocess.TimeoutExpired:
            record.state = "failed"
            record.error = "conversion process exceeded its release timeout"
        except OSError as exc:
            record.state = "failed"
            record.error = f"could not start quality adapter: {exc}"
        record.finished_at = utc_now()
        with self._lock:
            self._write_record(record)

    def output_files(self, job_id: str) -> list[dict[str, Any]]:
        record = self.get_job(job_id)
        if record is None:
            raise FileNotFoundError(job_id)
        root = Path(record.output_dir).resolve()
        if not root.is_dir():
            return []
        files: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            digest = _sha256_path(path)
            files.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                    "media_type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    "download_url": f"/v1/jobs/{job_id}/files/{relative}",
                }
            )
        return files

    def resolve_output_file(self, job_id: str, relative_path: str) -> Path:
        record = self.get_job(job_id)
        if record is None:
            raise FileNotFoundError(job_id)
        root = Path(record.output_dir).resolve()
        candidate = (root / relative_path).resolve()
        if candidate == root or root not in candidate.parents:
            raise PermissionError("output path escapes the job directory")
        if not candidate.is_file() or candidate.is_symlink():
            raise FileNotFoundError(relative_path)
        return candidate

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
