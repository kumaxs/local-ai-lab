"""Lifecycle helpers for queued conversion jobs.

The module is intentionally self-contained and does not depend on the full
docling-service runtime so it can be reused by API and job-management layers.
"""

from __future__ import annotations

import math
import os
import stat
import shutil
import threading
import time
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Protocol
from uuid import UUID


TERMINAL_STATES = {"succeeded", "failed", "interrupted"}
ACTIVE_STATES = {"queued", "running"}

CLEANUP_KIND_INPUT = "input"
CLEANUP_KIND_OUTPUT = "output"
CLEANUP_KIND_TOMBSTONE = "tombstone"
CLEANUP_KIND_TOMB_DIR = "tombstone_dir"
CLEANUP_KIND_STAGING = "staging_dir"
CLEANUP_KIND_TEMP = "temp_dir"
CLEANUP_KIND_ORPHAN_INPUT = "orphan_input"


class QueueFullError(RuntimeError):
    """Raised when the pending queue limit is reached."""


class StorageQuotaError(RuntimeError):
    """Raised when disk storage constraints are exceeded."""


class OutputTooLargeError(RuntimeError):
    """Raised when one job would exceed the per-job output policy."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Cleanup policy for terminal jobs and transient resources."""

    input_ttl: int = 24 * 60 * 60
    success_output_ttl: int = 7 * 24 * 60 * 60
    failed_output_ttl: int = 2 * 24 * 60 * 60
    tombstone_ttl: int = 30 * 24 * 60 * 60
    staging_ttl: int = 60 * 60
    temp_ttl: int = 60 * 60

    def __post_init__(self) -> None:
        for field_name in (
            "input_ttl",
            "success_output_ttl",
            "failed_output_ttl",
            "tombstone_ttl",
            "staging_ttl",
            "temp_ttl",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative number")


@dataclass(frozen=True)
class QuotaPolicy:
    """Submission and storage limits."""

    max_pending: int
    max_data_bytes: int
    min_free_bytes: int
    max_output_bytes: int


@dataclass
class JobRecord:
    """In-memory job snapshot used by lifecycle management."""

    job_id: str
    state: str
    input_path: str | Path
    output_path: str | Path
    created_at: float | int | str
    finished_at: float | int | str | None = None
    input_expires_at: float | int | str | None = None
    output_expires_at: float | int | str | None = None
    tombstone_expires_at: float | int | str | None = None
    tombstone_path: str | Path | None = None
    input_bytes: int = 0
    output_bytes: int = 0
    reserved_output_bytes: int | None = None


class StoreProtocol(Protocol):
    """Persistence access surface used by lifecycle maintenance components."""

    @abstractmethod
    def list_records(self) -> Iterable[JobRecord | Mapping[str, Any]]:
        """Return job snapshots that should be considered for lifecycle checks."""

    @abstractmethod
    def pending_and_bytes_stats(self) -> Mapping[str, Any] | list[Any] | tuple[Any, ...]:
        """Return atomic queue size and storage usage metrics."""

    @abstractmethod
    def claim_cleanup(self, job_id: str, kind: str, now: float) -> str | None:
        """Acquire a one-shot lock for a cleanup action."""

    @abstractmethod
    def complete_cleanup(
        self,
        job_id: str,
        kind: str,
        *,
        lease_id: str,
        deleted_bytes: int,
        error: str | None = None,
    ) -> None:
        """Report outcome for the claimed cleanup action."""


def _coerce_timestamp(value: float | int | str | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return float(__import__("datetime").datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def _coerce_int(value: int | str | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(value)


def _coerce_record(record: JobRecord | Mapping[str, Any]) -> JobRecord:
    if isinstance(record, JobRecord):
        return record
    return JobRecord(
        job_id=str(record.get("job_id")),
        state=str(record.get("state", "")),
        input_path=record.get("input_path", ""),
        output_path=record.get("output_path", ""),
        created_at=record.get("created_at", 0),
        finished_at=record.get("finished_at"),
        input_expires_at=record.get("input_expires_at"),
        output_expires_at=record.get("output_expires_at"),
        tombstone_expires_at=record.get("tombstone_expires_at"),
        tombstone_path=record.get("tombstone_path"),
        input_bytes=_coerce_int(record.get("input_bytes")),
        output_bytes=_coerce_int(record.get("output_bytes")),
        reserved_output_bytes=record.get("reserved_output_bytes"),
    )


def _coerce_cleanup_stats(stats: Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> tuple[int, int]:
    if isinstance(stats, Mapping):
        if "pending_count" in stats:
            pending = _coerce_int(stats.get("pending_count"))
        elif "pending_jobs" in stats:
            pending = _coerce_int(stats.get("pending_jobs"))
        elif "pending" in stats:
            pending = _coerce_int(stats.get("pending"))
        else:
            pending = 0
        if "reserved_bytes" in stats:
            total_bytes = _coerce_int(stats.get("reserved_bytes"))
        elif "used_bytes" in stats:
            total_bytes = _coerce_int(stats.get("used_bytes"))
        elif "bytes" in stats:
            total_bytes = _coerce_int(stats.get("bytes"))
        elif any(key in stats for key in ("input_bytes", "output_bytes", "reserved_output_bytes")):
            total_bytes = sum(
                _coerce_int(stats.get(key))
                for key in ("input_bytes", "output_bytes", "reserved_output_bytes")
            )
        else:
            total_bytes = _coerce_int(stats.get("total_bytes"))
        return pending, total_bytes

    if isinstance(stats, (list, tuple)):
        if len(stats) < 2:
            return 0, 0
        return _coerce_int(stats[0]), _coerce_int(stats[1])

    return 0, 0


def _coerce_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    return Path(path)


def _is_symlink_in_path(root: Path, target: Path) -> bool:
    try:
        rel = target.relative_to(root)
    except ValueError:
        return False
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value), version=4)
        return True
    except (TypeError, ValueError):
        return False


def safe_resolve(root: Path | str, target: Path | str) -> Path:
    """Resolve ``target`` and ensure it stays under ``root`` without symlinks."""

    root_for_scan = Path(root).absolute()
    if root_for_scan.is_symlink():
        raise PermissionError(f"refusing to use symlink root: {root_for_scan}")
    root_path = root_for_scan.resolve()
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = root_for_scan / target_path
    check_path = Path(os.path.normpath(str(target_path)))
    if not check_path.is_absolute():
        check_path = root_for_scan / check_path
    if _is_symlink_in_path(root_for_scan, check_path):
        raise PermissionError(f"refusing to follow symlink in path: {check_path}")

    resolved = target_path.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise PermissionError(f"path escapes root boundary: {target_path}") from exc

    return resolved


def open_relative_file(root: Path | str, relative_path: str) -> BinaryIO:
    """Open one regular file beneath ``root`` without following symlinks.

    POSIX callers are anchored to an open directory descriptor, closing the
    check/open race for both intermediate directories and the final file.  A
    conservative path-validation fallback is retained for platforms without
    ``dir_fd`` support.
    """

    root_path = Path(root).absolute()
    if root_path.is_symlink() or not root_path.is_dir():
        raise PermissionError(f"refusing unsafe file root: {root_path}")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
        or "\x00" in relative_path
        or relative_path.startswith("/")
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise PermissionError("relative file path is invalid")

    parts = relative_path.split("/")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        root_fd = os.open(root_path, os.O_RDONLY | directory | nofollow)
        directory_fds.append(root_fd)
        current_fd = root_fd
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | directory | nofollow,
                dir_fd=current_fd,
            )
            directory_fds.append(next_fd)
            current_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("output path is not a regular file")
        handle = os.fdopen(file_fd, "rb", closefd=True)
        file_fd = None
        return handle
    except (NotImplementedError, TypeError):
        candidate = safe_resolve(root_path, root_path.joinpath(*parts))
        if candidate.is_symlink() or not candidate.is_file():
            raise PermissionError("output path is not a regular file")
        return candidate.open("rb")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


def _dir_size(path: Path) -> int:
    total = 0
    for current in path.rglob("*"):
        if current.is_file() and not current.is_symlink():
            try:
                total += current.stat().st_size
            except OSError:
                continue
    return total


def safe_delete_tree(root: Path | str, target: Path | str) -> int:
    """Delete a single file or tree under ``root`` after hardening checks.

    Returns deleted byte count when deletion occurs. Missing paths return ``0``.
    """

    try:
        resolved = safe_resolve(root, target)
    except PermissionError as exc:
        raw_target = Path(target)
        if "symlink" in str(exc) and raw_target.is_symlink():
            # Removing the link itself is safe after independently validating
            # its parent; never resolve or traverse the link target.
            safe_resolve(root, raw_target.parent)
            raw_target.unlink(missing_ok=True)
            return 0
        raise
    if resolved == Path(root).resolve():
        raise PermissionError("refusing to delete root directory")

    if not resolved.exists():
        return 0

    if resolved.is_symlink() or resolved.is_socket():
        raise PermissionError(f"refusing to delete non-regular path: {resolved}")

    if resolved.is_file():
        size = resolved.stat().st_size
        resolved.unlink()
        return size

    if resolved.is_dir():
        size = _dir_size(resolved)
        shutil.rmtree(resolved)
        return size

    raise PermissionError(f"refusing to delete unsupported path type: {resolved}")


class QuotaManager:
    """Validate queue and storage capacity before accepting new work."""

    def __init__(
        self,
        policy: QuotaPolicy,
        *,
        disk_usage: Callable[[str], Any] = shutil.disk_usage,
    ) -> None:
        if policy.max_pending <= 0:
            raise ValueError("max_pending must be positive")
        if policy.max_data_bytes <= 0:
            raise ValueError("max_data_bytes must be positive")
        if policy.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if policy.min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")

        self.policy = policy
        self._disk_usage = disk_usage

    def check(
        self,
        store: StoreProtocol,
        input_bytes: int,
        data_root: Path,
        expected_output_bytes: int | None = None,
    ) -> None:
        """Raise quota exceptions when an incoming job would exceed policy."""

        expected_output = (
            self.policy.max_output_bytes if expected_output_bytes is None else expected_output_bytes
        )
        if expected_output > self.policy.max_output_bytes:
            raise OutputTooLargeError("output exceeds per-job maximum")

        if input_bytes > self.policy.max_data_bytes:
            raise StorageQuotaError("input exceeds configured max_data_bytes")

        pending_count, reserved_bytes = _coerce_cleanup_stats(store.pending_and_bytes_stats())
        if pending_count >= self.policy.max_pending:
            raise QueueFullError("pending queue is full")

        if reserved_bytes + input_bytes + expected_output > self.policy.max_data_bytes:
            raise StorageQuotaError("storage usage would exceed max_data_bytes")

        if self._disk_usage(str(data_root)).free < self.policy.min_free_bytes + expected_output:
            raise StorageQuotaError("free space would be below min_free_bytes")


class Janitor:
    """Background janitor that removes terminal-state job artifacts and temp files."""

    def __init__(
        self,
        store: StoreProtocol,
        *,
        retention: RetentionPolicy,
        input_root: Path,
        output_root: Path,
        tombstone_root: Path,
        staging_root: Path | None = None,
        temp_root: Path | None = None,
        download_lease: Callable[[str], bool] | None = None,
        scan_interval_seconds: float = 300.0,
        now: Callable[[], float] = time.time,
        cleanup_delete_fn: Callable[[Path, Path | str], int] | None = None,
        pending_inputs: Callable[[], set[str]] | None = None,
        protected_temp_entries: Callable[[], set[str]] | None = None,
        maintenance: Iterable[Callable[[], Any]] = (),
    ) -> None:
        self._store = store
        self._retention = retention
        self._input_root = input_root
        self._output_root = output_root
        self._tombstone_root = tombstone_root
        self._staging_root = staging_root
        self._temp_root = temp_root
        self._download_lease = download_lease or (lambda _job_id: False)
        if (
            isinstance(scan_interval_seconds, bool)
            or not isinstance(scan_interval_seconds, (int, float))
            or not math.isfinite(float(scan_interval_seconds))
            or scan_interval_seconds <= 0
        ):
            raise ValueError("scan_interval_seconds must be a positive number")
        self._interval = float(scan_interval_seconds)
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delete_fn = cleanup_delete_fn or safe_delete_tree
        self._pending_inputs = pending_inputs or (lambda: set())
        self._protected_temp_entries = protected_temp_entries or (lambda: set())
        self._maintenance = tuple(maintenance)
        self._config_lock = threading.RLock()
        self._wake = threading.Event()

    @property
    def retention(self) -> RetentionPolicy:
        with self._config_lock:
            return self._retention

    @property
    def scan_interval_seconds(self) -> float:
        with self._config_lock:
            return float(self._interval)

    def _is_active_download(self, job_id: str) -> bool:
        return bool(self._download_lease(job_id))

    def _active_job_ids(self) -> set[str]:
        active: set[str] = set()
        for record in self._store.list_records():
            normalized = _coerce_record(record)
            if normalized.state in ACTIVE_STATES:
                active.add(normalized.job_id)
        return active

    def _all_job_ids(self) -> set[str]:
        jobs: set[str] = set()
        for record in self._store.list_records():
            normalized = _coerce_record(record)
            if _is_uuid(normalized.job_id):
                jobs.add(normalized.job_id)
        return jobs

    def _coerce_finish_or_create(
        self,
        *,
        record: JobRecord | None = None,
    ) -> float | None:
        if record is None:
            return None
        base = _coerce_timestamp(record.finished_at)
        if base is not None:
            return base
        return _coerce_timestamp(record.created_at)

    def _expiry_for_input(self, record: JobRecord) -> float | None:
        with self._config_lock:
            retention = self._retention
        return self._expiry_for_input_with_retention(record, retention)

    def _expiry_for_input_with_retention(self, record: JobRecord, retention: RetentionPolicy) -> float | None:
        if record.input_expires_at is not None:
            return _coerce_timestamp(record.input_expires_at)
        base = self._coerce_finish_or_create(record=record)
        if base is None:
            return None
        return base + retention.input_ttl

    def _expiry_for_output(self, record: JobRecord) -> float | None:
        with self._config_lock:
            retention = self._retention
        return self._expiry_for_output_with_retention(record, retention)

    def _expiry_for_output_with_retention(self, record: JobRecord, retention: RetentionPolicy) -> float | None:
        if record.output_expires_at is not None:
            return _coerce_timestamp(record.output_expires_at)
        base = self._coerce_finish_or_create(record=record)
        if base is None:
            return None
        ttl = (
            retention.success_output_ttl
            if record.state == "succeeded"
            else retention.failed_output_ttl
        )
        return base + ttl

    def _expiry_for_tombstone(self, record: JobRecord) -> float | None:
        with self._config_lock:
            retention = self._retention
        return self._expiry_for_tombstone_with_retention(record, retention)

    def _expiry_for_tombstone_with_retention(self, record: JobRecord, retention: RetentionPolicy) -> float | None:
        if record.tombstone_expires_at is not None:
            return _coerce_timestamp(record.tombstone_expires_at)
        base = self._coerce_finish_or_create(record=record)
        if base is None:
            return None
        return base + retention.tombstone_ttl

    def _cleanup_component(
        self,
        *,
        job_id: str,
        kind: str,
        root: Path,
        path: Path | str | None,
        now: float,
    ) -> None:
        if path is None:
            return

        lease_id = self._store.claim_cleanup(job_id, kind, now)
        if not lease_id:
            return

        try:
            deleted = self._delete_fn(root, path)
        except Exception as exc:  # pragma: no cover - defensive branch
            self._store.complete_cleanup(
                job_id,
                kind,
                lease_id=lease_id,
                deleted_bytes=0,
                error=str(exc),
            )
        else:
            self._store.complete_cleanup(
                job_id,
                kind,
                lease_id=lease_id,
                deleted_bytes=deleted,
                error=None,
            )

    def _cleanup_record(
        self,
        record: JobRecord,
        now: float,
        retention: RetentionPolicy | None = None,
    ) -> None:
        if self._is_active_download(record.job_id):
            return

        if retention is None:
            with self._config_lock:
                retention = self._retention

        input_expiry = self._expiry_for_input_with_retention(record, retention)
        if input_expiry is not None and now >= input_expiry:
            input_path = _coerce_path(record.input_path)
            if (
                input_path is not None
                and not input_path.is_dir()
                and input_path.suffix.casefold() == ".pdf"
                and input_path.parent != self._input_root
            ):
                input_path = input_path.parent
            self._cleanup_component(
                job_id=record.job_id,
                kind=CLEANUP_KIND_INPUT,
                root=self._input_root,
                path=input_path,
                now=now,
            )

        output_expiry = self._expiry_for_output_with_retention(record, retention)
        if output_expiry is not None and now >= output_expiry:
            self._cleanup_component(
                job_id=record.job_id,
                kind=CLEANUP_KIND_OUTPUT,
                root=self._output_root,
                path=_coerce_path(record.output_path),
                now=now,
            )

        tombstone_expiry = self._expiry_for_tombstone_with_retention(record, retention)
        if tombstone_expiry is not None and now >= tombstone_expiry and record.tombstone_path is not None:
            self._cleanup_component(
                job_id=record.job_id,
                kind=CLEANUP_KIND_TOMBSTONE,
                root=self._tombstone_root,
                path=_coerce_path(record.tombstone_path),
                now=now,
            )

    def _cleanup_directory(self, root: Path | None, ttl_seconds: int, now: float, kind: str, active_ids: set[str]) -> None:
        if root is None or not root.exists():
            return

        for entry in list(root.iterdir()):
            if not entry.exists():
                continue
            if kind in {CLEANUP_KIND_STAGING, CLEANUP_KIND_TEMP} and entry.name in active_ids:
                continue
            if kind == CLEANUP_KIND_ORPHAN_INPUT:
                if entry.is_symlink() or not entry.is_dir() or entry.name in active_ids or not _is_uuid(entry.name):
                    continue

            try:
                mtime = os.path.getmtime(entry)
            except OSError:
                continue

            if now - mtime < ttl_seconds:
                continue

            lease_id = self._store.claim_cleanup(entry.name, kind, now)
            if not lease_id:
                continue

            try:
                deleted = self._delete_fn(root, entry)
            except Exception as exc:
                self._store.complete_cleanup(
                    entry.name,
                    kind,
                    lease_id=lease_id,
                    deleted_bytes=0,
                    error=str(exc),
                )
            else:
                self._store.complete_cleanup(
                    entry.name,
                    kind,
                    lease_id=lease_id,
                    deleted_bytes=deleted,
                    error=None,
                )

    def _cleanup_directory_with_retention(
        self,
        root: Path | None,
        ttl_seconds: int,
        now: float,
        kind: str,
        active_ids: set[str],
    ) -> None:
        self._cleanup_directory(root, ttl_seconds, now, kind, active_ids)

    def run_once(self) -> None:
        # Capture one immutable policy snapshot so a concurrent reconfigure
        # cannot produce a half-old/half-new cleanup pass.  Explicit expiry
        # timestamps on existing jobs are always honored by the helpers above.
        with self._config_lock:
            retention = self._retention
        now = self._now()
        active_job_ids = self._active_job_ids()
        all_job_ids = self._all_job_ids()
        pending_input_ids = set(self._pending_inputs())
        protected_temp_entries = set(self._protected_temp_entries())

        for record in list(self._store.list_records()):
            normalized = _coerce_record(record)
            if normalized.state not in TERMINAL_STATES:
                continue
            self._cleanup_record(normalized, now, retention)

        self._cleanup_directory(
            self._input_root,
            retention.temp_ttl,
            now,
            CLEANUP_KIND_ORPHAN_INPUT,
            all_job_ids.union(pending_input_ids),
        )

        self._cleanup_directory(
            self._tombstone_root,
            retention.tombstone_ttl,
            now,
            CLEANUP_KIND_TOMB_DIR,
            active_job_ids,
        )
        self._cleanup_directory(
            self._staging_root,
            retention.staging_ttl,
            now,
            CLEANUP_KIND_STAGING,
            active_job_ids,
        )
        self._cleanup_directory(
            self._temp_root,
            retention.temp_ttl,
            now,
            CLEANUP_KIND_TEMP,
            protected_temp_entries,
        )
        for callback in self._maintenance:
            try:
                callback()
            except Exception:
                # A failed purge must not terminate the background janitor;
                # the same callback is retried on the next scan.
                continue

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._config_lock:
                interval = max(0.0, float(self._interval))
            # A reconfigure/wake only interrupts the wait.  It deliberately
            # does not run cleanup immediately; the next full interval uses
            # the new policy, avoiding surprise purges from an API PATCH.
            signaled = self._wake.wait(interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            if signaled:
                continue
            try:
                self.run_once()
            except Exception:
                # A single filesystem or store failure must not kill the
                # background janitor.  Its next interval retries the pass.
                continue

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, *, wait: float | None = 1.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is None:
            return
        self._thread.join(timeout=wait)

    def reconfigure(
        self,
        *,
        retention: RetentionPolicy | None = None,
        scan_interval_seconds: float | None = None,
        **retention_updates: int,
    ) -> None:
        """Atomically apply a new policy and wake a sleeping janitor.

        ``retention`` can replace the complete dataclass, while keyword TTLs
        (``input_ttl``, ``success_output_ttl``, …) are convenient for callers
        that only changed one value.  The wake is intentionally non-destructive
        and never invokes :meth:`run_once` inline.
        """
        with self._config_lock:
            current = self._retention
            if retention is not None and retention_updates:
                raise ValueError("pass retention or individual TTLs, not both")
            if retention_updates:
                allowed = {
                    "input_ttl",
                    "success_output_ttl",
                    "failed_output_ttl",
                    "tombstone_ttl",
                    "staging_ttl",
                    "temp_ttl",
                }
                unknown = set(retention_updates).difference(allowed)
                if unknown:
                    raise ValueError(f"unknown retention field: {sorted(unknown)[0]}")
                retention = dataclass_replace(current, **retention_updates)
            if retention is not None:
                if not isinstance(retention, RetentionPolicy):
                    raise TypeError("retention must be a RetentionPolicy")
                self._retention = retention
            if scan_interval_seconds is not None:
                if (
                    isinstance(scan_interval_seconds, bool)
                    or not isinstance(scan_interval_seconds, (int, float))
                    or not math.isfinite(float(scan_interval_seconds))
                    or scan_interval_seconds <= 0
                ):
                    raise ValueError(
                        "scan_interval_seconds must be a positive number"
                    )
                self._interval = float(scan_interval_seconds)
        self._wake.set()

    def wake(self) -> None:
        """Interrupt the current wait without triggering an immediate purge."""
        self._wake.set()
