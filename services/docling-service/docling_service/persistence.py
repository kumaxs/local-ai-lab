"""SQLite persistence layer for docling-service jobs.

The implementation keeps a strict v1 schema and exposes a stable API used by
service orchestration, lifecycle maintenance, and webhook dispatch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_iso(value: Any, *, default_now: bool = False) -> str | None:
    if value is None:
        return _utc_now() if default_now else None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


def _to_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _from_cursor(cursor: str) -> dict[str, Any]:
    decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    payload = json.loads(decoded)
    if not isinstance(payload, dict):
        raise ValueError("cursor payload must be a mapping")
    return payload


def _build_placeholders(values: Sequence[Any]) -> str:
    if not values:
        return "(NULL)"
    return "(" + ", ".join("?" for _ in values) + ")"


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _coerce_json(raw: str | None) -> dict[str, Any] | list[Any] | None:
    if raw is None:
        return None
    parsed = json.loads(raw)
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _coerce_bool_to_int(value: Any) -> int:
    return 1 if bool(value) else 0


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_MIGRATION_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


class SQLiteStore:
    VALID_STATES = {"queued", "running", "succeeded", "failed", "interrupted"}
    TERMINAL_STATES = {"succeeded", "failed", "interrupted"}
    PENDING_STATES = {"queued", "running"}
    LEGACY_FIELDS = (
        "job_id",
        "state",
        "original_name",
        "input_path",
        "output_dir",
        "created_at",
        "started_at",
        "finished_at",
        "exit_code",
        "error",
    )
    WEBHOOK_DELIVERY_STATUSES = {"pending", "in_progress", "retrying", "succeeded", "failed"}

    DEFAULT_WEBHOOK_MAX_ATTEMPTS = 6
    DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
    DEFAULT_CLEANUP_LEASE_SECONDS = 300

    def __init__(
        self,
        db_path: str | Path,
        *,
        input_root: str | Path | None = None,
        output_root: str | Path | None = None,
        max_pending: int | None = None,
        max_data_bytes: int | None = None,
        webhook_max_attempts: int = DEFAULT_WEBHOOK_MAX_ATTEMPTS,
        max_webhook_subscriptions: int | None = None,
    ) -> None:
        self._path = Path(db_path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._input_root = Path(input_root).resolve() if input_root else None
        self._output_root = Path(output_root).resolve() if output_root else None
        self._webhook_max_attempts = webhook_max_attempts
        self._max_pending = max_pending if (max_pending and max_pending > 0) else None
        self._max_data_bytes = (
            max_data_bytes if (max_data_bytes and max_data_bytes > 0) else None
        )
        self._max_webhook_subscriptions = (
            max_webhook_subscriptions
            if max_webhook_subscriptions and max_webhook_subscriptions > 0
            else None
        )

        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=8000;")

        self._lock = threading.RLock()
        self._ensure_schema()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()

    @contextmanager
    def _write_txn(self):
        with self._lock:
            nested = self._conn.in_transaction
            if not nested:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                if not nested:
                    self._conn.rollback()
                raise
            else:
                if not nested:
                    self._conn.commit()

    def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def _to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _coerce_payload(raw: str | None) -> Any:
        return _coerce_json(raw)

    def _validate_rooted_path(self, path: str, root: Path | None, label: str) -> None:
        if root is None:
            return
        target = Path(path).resolve()
        if target == root or root in target.parents:
            return
        raise ValueError(f"{label} must be under {root}")

    def _ensure_schema(self) -> None:
        key = str(self._path)
        with _LOCKS_LOCK:
            lock = _MIGRATION_LOCKS.setdefault(key, threading.Lock())

        with lock:
            with self._write_txn():
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT NOT NULL PRIMARY KEY,
                        state TEXT NOT NULL CHECK (
                            state IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
                        ),
                        original_name TEXT NOT NULL,
                        client_reference TEXT,
                        input_path TEXT NOT NULL,
                        output_dir TEXT NOT NULL,
                        input_sha256 TEXT,
                        input_size_bytes INTEGER NOT NULL DEFAULT 0,
                        output_size_bytes INTEGER NOT NULL DEFAULT 0,
                        manifest_version INTEGER NOT NULL DEFAULT 0,
                        manifest_sha256 TEXT,
                        input_expires_at TEXT,
                        output_expires_at TEXT,
                        tombstone_expires_at TEXT,
                        tombstoned_at TEXT,
                        tombstone_reason TEXT,
                        reserved_output_bytes INTEGER NOT NULL DEFAULT 0,
                        input_deleted_at TEXT,
                        output_deleted_at TEXT,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        delete_requested_at TEXT,
                        deleted_at TEXT,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        exit_code INTEGER,
                        error TEXT,
                        updated_at TEXT NOT NULL
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS job_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256 TEXT,
                        media_type TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        expires_at TEXT,
                        deleted_at TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
                        UNIQUE(job_id, path)
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS idempotency_keys (
                        idempotency_key TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        request_fingerprint TEXT,
                        expires_at TEXT,
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
                        UNIQUE(job_id)
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS download_leases (
                        lease_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        holder TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        released_at TEXT,
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
                        UNIQUE(job_id, relative_path)
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        callback_url TEXT NOT NULL,
                        event_types TEXT NOT NULL,
                        filters TEXT NOT NULL,
                        secret TEXT,
                        headers TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        name TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS webhook_deliveries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subscription_id INTEGER NOT NULL,
                        job_id TEXT,
                        event_type TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL,
                        max_attempts INTEGER NOT NULL DEFAULT 6,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT NOT NULL,
                        locked_until TEXT,
                        locked_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_error TEXT,
                        last_status_code INTEGER,
                        last_response TEXT,
                        FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE SET NULL,
                        UNIQUE(subscription_id, event_id)
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cleanup_claims (
                        job_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        lease_id TEXT NOT NULL,
                        claimed_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        completed_at TEXT,
                        last_error TEXT,
                        last_deleted_bytes INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(job_id, kind)
                    )
                    """
                )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_sync (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        source_hash TEXT,
                        imported_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 6,
                        next_attempt_at TEXT NOT NULL,
                        locked_until TEXT,
                        locked_by TEXT,
                        completed_at TEXT,
                        last_error TEXT,
                        last_synced_at TEXT,
                        updated_at TEXT NOT NULL,
                        legacy_payload TEXT,
                        UNIQUE(job_id),
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                    )
                    """
                )

                self._conn.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
                    CREATE INDEX IF NOT EXISTS idx_jobs_client_reference ON jobs(client_reference);
                    CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC, job_id DESC);
                    CREATE INDEX IF NOT EXISTS idx_jobs_tombstoned ON jobs(tombstoned_at);
                    CREATE INDEX IF NOT EXISTS idx_job_files_job_id ON job_files(job_id);
                    CREATE INDEX IF NOT EXISTS idx_job_files_status ON job_files(status);
                    CREATE INDEX IF NOT EXISTS idx_job_files_expires ON job_files(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_idempotency_job ON idempotency_keys(job_id);
                    CREATE INDEX IF NOT EXISTS idx_download_leases_expires ON download_leases(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_webhook_sub_del_status ON webhook_deliveries(status, next_attempt_at);
                    CREATE INDEX IF NOT EXISTS idx_webhook_sub_job ON webhook_deliveries(job_id);
                    CREATE INDEX IF NOT EXISTS idx_webhook_sub_event ON webhook_deliveries(event_id);
                    CREATE INDEX IF NOT EXISTS idx_legacy_sync_status ON legacy_sync(status, next_attempt_at);
                    CREATE INDEX IF NOT EXISTS idx_cleanup_claims_expires ON cleanup_claims(expires_at);
                    """
                )

                self._conn.execute(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at)
                    VALUES (1, 'v1', ?)
                    ON CONFLICT(version) DO UPDATE SET
                        name = excluded.name,
                        applied_at = excluded.applied_at
                    """,
                    (_utc_now(),),
                )

                job_columns = {
                    row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")
                }
                if "manifest_sha256" not in job_columns:
                    self._conn.execute("ALTER TABLE jobs ADD COLUMN manifest_sha256 TEXT")
                legacy_columns = {
                    row["name"] for row in self._conn.execute("PRAGMA table_info(legacy_sync)")
                }
                if "updated_at" not in legacy_columns:
                    self._conn.execute("ALTER TABLE legacy_sync ADD COLUMN updated_at TEXT")
                    self._conn.execute(
                        "UPDATE legacy_sync SET updated_at = COALESCE(imported_at, ?)",
                        (_utc_now(),),
                    )

    def _row_to_job(self, row: sqlite3.Row | None) -> dict[str, Any]:
        data = self._to_dict(row)
        if not data:
            return {}
        if data.get("deleted") is not None:
            data["deleted"] = bool(data["deleted"])
        for name in (
            "input_size_bytes",
            "output_size_bytes",
            "reserved_output_bytes",
            "manifest_version",
            "exit_code",
        ):
            if name in data and data[name] is not None:
                data[name] = _coerce_int(data[name])
        data["output_dir"] = data.get("output_dir")
        data["output_path"] = data.get("output_dir")
        return data

    def _row_to_manifest_item(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        payload = self._to_dict(row)
        if not payload:
            return None
        payload.pop("id", None)
        return payload

    def _validate_manifest(
        self, manifest: Sequence[Mapping[str, Any]]
    ) -> list[tuple[str, int, str | None, str | None, str, str | None, str | None]]:
        entries: list[tuple[str, int, str | None, str | None, str, str | None, str | None]] = []
        seen: set[str] = set()
        for index, item in enumerate(manifest):
            if not isinstance(item, Mapping):
                raise ValueError("invalid_manifest_item")
            path = item.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("invalid_path")
            posix_path = PurePosixPath(path)
            if (
                "\\" in path
                or "\x00" in path
                or posix_path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or path in seen
            ):
                raise ValueError("invalid_path")
            seen.add(path)
            size_bytes = item.get("size_bytes")
            if not isinstance(size_bytes, int) or size_bytes < 0:
                raise ValueError("invalid_size_bytes")
            sha = item.get("sha256")
            if sha is not None and (
                not isinstance(sha, str)
                or len(sha) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in sha)
            ):
                raise ValueError("invalid_sha256")
            media_type = item.get("media_type")
            status = item.get("status", "active")
            if status not in {"active", "archived", "deleted"}:
                raise ValueError("invalid_status")
            expires_at = item.get("expires_at")
            if expires_at is not None and not isinstance(expires_at, str):
                raise ValueError("invalid_expires_at")
            deleted_at = item.get("deleted_at")
            if deleted_at is not None and not isinstance(deleted_at, str):
                raise ValueError("invalid_deleted_at")
            entries.append((path, int(size_bytes), str(sha) if sha is not None else None, str(media_type) if media_type is not None else None, str(status), expires_at, deleted_at))
        return entries

    def _replace_manifest_txn(self, job_id: str, manifest: Sequence[Mapping[str, Any]], now: str, *, clear: bool = True) -> tuple[int, int]:
        entries = self._validate_manifest(manifest)
        if clear:
            self._conn.execute("DELETE FROM job_files WHERE job_id = ?", (job_id,))
        total = 0
        if entries:
            records = [
                (
                    job_id,
                    path,
                    size,
                    sha,
                    media,
                    status,
                    expires,
                    deleted,
                    now,
                )
                for path, size, sha, media, status, expires, deleted in entries
            ]
            self._conn.executemany(
                """
                INSERT INTO job_files (
                    job_id, path, size_bytes, sha256, media_type,
                    status, expires_at, deleted_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, path)
                DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    media_type = excluded.media_type,
                    status = excluded.status,
                    expires_at = excluded.expires_at,
                    deleted_at = excluded.deleted_at,
                    created_at = excluded.created_at
                """,
                records,
            )
            total = sum(size for _, size, _, _, _, _, _ in entries)
        canonical = [
            {
                "path": path,
                "size_bytes": size,
                "sha256": sha,
                "media_type": media,
                "status": status,
                "expires_at": expires,
                "deleted_at": deleted,
            }
            for path, size, sha, media, status, expires, deleted in sorted(entries)
        ]
        manifest_sha256 = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._conn.execute(
            """
            UPDATE jobs
            SET output_size_bytes = ?, manifest_sha256 = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (total, manifest_sha256, now, job_id),
        )
        return len(entries), total

    # --------------------
    # Job operations
    # --------------------
    def create_job(
        self,
        *,
        job_id: str,
        original_name: str,
        input_path: str,
        output_dir: str,
        state: str = "queued",
        client_reference: str | None = None,
        input_sha256: str | None = None,
        input_size_bytes: int | None = None,
        output_size_bytes: int | None = None,
        reserved_output_bytes: int | None = None,
        input_expires_at: str | None = None,
        output_expires_at: str | None = None,
        tombstone_expires_at: str | None = None,
        created_at: str | None = None,
        manifest_version: int = 0,
    ) -> dict[str, Any]:
        if not _is_uuid(job_id):
            return {"error": "invalid_job_id"}
        if state not in self.VALID_STATES:
            return {"error": "invalid_state"}

        created = created_at or _utc_now()
        self._validate_rooted_path(input_path, self._input_root, "input_path")
        self._validate_rooted_path(output_dir, self._output_root, "output_dir")

        with self._write_txn():
            existing = self._to_dict(self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,)))
            if existing:
                return {"error": "job_id_conflict"}

            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, state, original_name, client_reference, input_path,
                    output_dir, input_sha256, input_size_bytes, output_size_bytes,
                    manifest_version, input_expires_at, output_expires_at,
                    tombstone_expires_at, tombstoned_at, tombstone_reason,
                    reserved_output_bytes, input_deleted_at, output_deleted_at, deleted,
                    delete_requested_at, deleted_at, created_at, started_at, finished_at,
                    exit_code, error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, 0, NULL, NULL, ?, NULL, NULL, NULL, NULL, ?)
                """,
                (
                    job_id,
                    state,
                    original_name,
                    client_reference,
                    str(Path(input_path)),
                    str(Path(output_dir)),
                    input_sha256,
                    int(input_size_bytes or 0),
                    int(output_size_bytes or 0),
                    int(manifest_version),
                    input_expires_at,
                    output_expires_at,
                    tombstone_expires_at,
                    int(reserved_output_bytes or 0),
                    created,
                    created,
                ),
            )
            return self._row_to_job(
                self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            )

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not _is_uuid(job_id):
            return {}
        return self._row_to_job(self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,)))

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        if not _is_uuid(job_id):
            return {"error": "invalid_job_id"}

        allowed = {
            "state",
            "original_name",
            "client_reference",
            "input_path",
            "output_dir",
            "input_sha256",
            "input_size_bytes",
            "output_size_bytes",
            "manifest_version",
            "input_expires_at",
            "output_expires_at",
            "tombstone_expires_at",
            "tombstoned_at",
            "tombstone_reason",
            "reserved_output_bytes",
            "input_deleted_at",
            "output_deleted_at",
            "deleted",
            "delete_requested_at",
            "deleted_at",
            "started_at",
            "finished_at",
            "exit_code",
            "error",
        }

        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_job(job_id)

        if "input_path" in updates:
            self._validate_rooted_path(str(updates["input_path"]), self._input_root, "input_path")
        if "output_dir" in updates:
            self._validate_rooted_path(str(updates["output_dir"]), self._output_root, "output_dir")
        if "state" in updates and updates["state"] not in self.VALID_STATES:
            return {"error": "invalid_state"}
        expected_state: str | None = None
        if "state" in updates:
            current = self.get_job(job_id)
            if not current:
                return {"error": "job_not_found"}
            expected_state = str(current.get("state"))
            allowed_transitions = {
                "queued": self.VALID_STATES,
                "running": {"running", *self.TERMINAL_STATES},
                "succeeded": {"succeeded"},
                "failed": {"failed"},
                "interrupted": {"interrupted"},
            }
            if updates["state"] not in allowed_transitions.get(expected_state, set()):
                return {"error": "job_state_conflict"}

        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            values.append(value)

        assignments.append("updated_at = ?")
        values.append(_utc_now())
        values.append(job_id)

        with self._write_txn():
            where = "job_id = ?"
            if expected_state is not None:
                where += " AND state = ?"
                values.append(expected_state)
            result = self._conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE {where}",
                values,
            )
            if result.rowcount == 0:
                return {"error": "job_state_conflict" if expected_state else "job_not_found"}
        return self.get_job(job_id)

    def create_job_with_idempotency(
        self,
        *,
        idempotency_key: str,
        job_id: str,
        request_fingerprint: str | None = None,
        idempotency_ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
        **job_fields: Any,
    ) -> dict[str, Any]:
        if not idempotency_key:
            return {"error": "invalid_idempotency_key"}
        if not _is_uuid(job_id):
            return {"error": "invalid_job_id"}

        now = _utc_now()

        with self._write_txn():
            row = self._to_dict(
                self._fetch_one(
                    "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
            )
            if row:
                expires_at = _parse_datetime(row.get("expires_at"))
                if expires_at is not None and expires_at <= datetime.now(timezone.utc):
                    self._conn.execute(
                        "DELETE FROM idempotency_keys WHERE idempotency_key = ?",
                        (idempotency_key,),
                    )
                    row = {}
            if row:
                if (row.get("request_fingerprint") or None) != (request_fingerprint or None):
                    return {"error": "idempotency_conflict"}
                replay = self.get_job(row["job_id"])
                replay["_idempotent_replay"] = True
                return replay

            if self._max_pending is not None:
                pending = self._fetch_one(
                    "SELECT COUNT(*) AS count FROM jobs WHERE state IN (?, ?) AND tombstoned_at IS NULL",
                    tuple(sorted(self.PENDING_STATES)),
                )
                if _coerce_int(pending["count"] if pending else 0) >= self._max_pending:
                    return {"error": "queue_full"}

            if self._max_data_bytes is not None:
                totals = self._fetch_one(
                    """
                    SELECT COALESCE(SUM(
                        input_size_bytes + output_size_bytes + reserved_output_bytes
                    ), 0) AS used_bytes
                    FROM jobs
                    WHERE deleted = 0
                    """
                )
                requested = (
                    _coerce_int(job_fields.get("input_size_bytes"))
                    + _coerce_int(job_fields.get("reserved_output_bytes"))
                )
                if _coerce_int(totals["used_bytes"] if totals else 0) + requested > self._max_data_bytes:
                    return {"error": "quota_exceeded"}

            created = self.create_job(
                job_id=job_id,
                original_name=str(job_fields.get("original_name", "document.pdf")),
                input_path=str(job_fields.get("input_path", f"{self._input_root or ''}/{uuid4()}.bin")),
                output_dir=str(job_fields.get("output_dir", f"{self._output_root or ''}/{uuid4()}")),
                state=str(job_fields.get("state", "queued")),
                client_reference=job_fields.get("client_reference"),
                input_sha256=job_fields.get("input_sha256"),
                input_size_bytes=_coerce_int(job_fields.get("input_size_bytes") ),
                output_size_bytes=_coerce_int(job_fields.get("output_size_bytes") ),
                reserved_output_bytes=_coerce_int(job_fields.get("reserved_output_bytes")),
                input_expires_at=job_fields.get("input_expires_at"),
                output_expires_at=job_fields.get("output_expires_at"),
                tombstone_expires_at=job_fields.get("tombstone_expires_at"),
                created_at=job_fields.get("created_at", now),
                manifest_version=_coerce_int(job_fields.get("manifest_version", 0)),
            )

            if created.get("error"):
                return created

            expires = datetime.fromisoformat(now) + timedelta(
                seconds=max(60, idempotency_ttl_seconds)
            )
            self._conn.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key, job_id, created_at, request_fingerprint, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (idempotency_key, job_id, now, request_fingerprint, expires.isoformat()),
            )
            created["_idempotent_replay"] = False
            return created

    def list_jobs(
        self,
        *,
        state: str | None = None,
        client_reference: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        include_tombstoned: bool = False,
    ) -> dict[str, Any]:
        if limit < 1:
            limit = 1

        criteria: list[str] = []
        params: list[Any] = []

        if state:
            criteria.append("state = ?")
            params.append(state)

        if client_reference is not None:
            criteria.append("client_reference = ?")
            params.append(client_reference)

        if created_after:
            criteria.append("created_at >= ?")
            params.append(created_after)

        if created_before:
            criteria.append("created_at < ?")
            params.append(created_before)

        if not include_tombstoned:
            criteria.append("tombstoned_at IS NULL")
            criteria.append("deleted = 0")

        if cursor:
            try:
                decoded = _from_cursor(cursor)
            except Exception:
                return {"error": "invalid_cursor"}

            marker_created = decoded.get("created_at")
            marker_job_id = decoded.get("job_id")
            if not marker_created or not marker_job_id:
                return {"error": "invalid_cursor"}

            if "state" not in decoded or decoded.get("state") != state:
                return {"error": "invalid_cursor"}
            if (
                "client_reference" not in decoded
                or decoded.get("client_reference") != client_reference
            ):
                return {"error": "invalid_cursor"}

            criteria.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
            params.extend([marker_created, marker_created, marker_job_id])

        where = f"WHERE {' AND '.join(criteria)}" if criteria else ""

        rows = self._fetch(
            f"""
            SELECT * FROM jobs
            {where}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            tuple(params + [limit + 1]),
        )

        items = [self._row_to_job(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = _to_cursor(
                {
                    "created_at": last["created_at"],
                    "job_id": last["job_id"],
                    "state": state,
                    "client_reference": client_reference,
                }
            )

        return {"items": items, "count": len(items), "next_cursor": next_cursor, "limit": limit}

    def replace_manifest(
        self,
        job_id: str,
        manifest: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not manifest or not isinstance(manifest, Sequence):
            return {"error": "invalid_manifest"}

        if not self.get_job(job_id):
            return {"error": "job_not_found"}

        now = _utc_now()
        with self._write_txn():
            try:
                count, total = self._replace_manifest_txn(job_id, manifest, now)
            except ValueError as exc:
                return {"error": str(exc)}

        result = self.list_manifest(job_id, include_deleted=True)
        result["total_size_bytes"] = total
        return result

    def list_manifest(
        self,
        job_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        if not self.get_job(job_id):
            return {"error": "job_not_found"}

        where = "job_id = ?"
        params = [job_id]
        if not include_deleted:
            where += " AND status != 'deleted'"

        rows = self._fetch(
            f"""
            SELECT path, size_bytes, sha256, media_type, status, expires_at, deleted_at, created_at
            FROM job_files
            WHERE {where}
            ORDER BY path ASC
            """,
            tuple(params),
        )

        items = [self._to_dict(row) for row in rows]
        return {"job_id": job_id, "items": items, "count": len(items)}

    def pending_and_bytes_stats(self) -> dict[str, Any]:
        pending_states = tuple(sorted(self.PENDING_STATES))

        pending_jobs = self._fetch_one(
            f"""
            SELECT COUNT(*) AS pending_jobs
            FROM jobs
            WHERE state IN {_build_placeholders(pending_states)}
              AND tombstoned_at IS NULL
              AND deleted = 0
            """,
            pending_states,
        )

        pending_bytes = self._fetch_one(
            f"""
            SELECT COALESCE(SUM(jf.size_bytes), 0) AS pending_bytes
            FROM job_files AS jf
            JOIN jobs AS j ON j.job_id = jf.job_id
            WHERE jf.status != 'deleted'
              AND j.state IN {_build_placeholders(pending_states)}
              AND j.tombstoned_at IS NULL
              AND j.deleted = 0
            """,
            pending_states,
        )

        stored_bytes = self._fetch_one(
            "SELECT COALESCE(SUM(size_bytes), 0) AS stored_bytes FROM job_files WHERE status != 'deleted'",
        )

        by_state_rows = self._fetch(
            """
            SELECT state, COUNT(*) AS count
            FROM jobs
            WHERE tombstoned_at IS NULL
            GROUP BY state
            ORDER BY state ASC
            """
        )
        by_state = {row["state"]: int(row["count"]) for row in by_state_rows}

        totals = self._fetch_one(
            """
            SELECT
                COALESCE(SUM(input_size_bytes), 0) AS input_bytes,
                COALESCE(SUM(output_size_bytes), 0) AS output_bytes,
                COALESCE(SUM(reserved_output_bytes), 0) AS reserved_output_bytes
            FROM jobs
            WHERE deleted = 0
            """
        )

        pending_file = self._fetch_one(
            f"""
            SELECT
                COALESCE(SUM(j.input_size_bytes), 0) AS input_bytes,
                COALESCE(SUM(j.output_size_bytes), 0) AS output_bytes
            FROM jobs AS j
            WHERE j.state IN {_build_placeholders(pending_states)}
              AND j.tombstoned_at IS NULL
              AND j.deleted = 0
            """,
            pending_states,
        )

        return {
            "pending_jobs": int(pending_jobs["pending_jobs"]) if pending_jobs else 0,
            "pending_bytes": int(pending_bytes["pending_bytes"]) if pending_bytes else 0,
            "stored_bytes": int(stored_bytes["stored_bytes"]) if stored_bytes else 0,
            "jobs_by_state": by_state,
            "input_bytes": int(totals["input_bytes"]) if totals else 0,
            "output_bytes": int(totals["output_bytes"]) if totals else 0,
            "reserved_output_bytes": int(totals["reserved_output_bytes"]) if totals else 0,
            "pending_input_bytes": int(pending_file["input_bytes"]) if pending_file else 0,
            "pending_output_bytes": int(pending_file["output_bytes"]) if pending_file else 0,
            "pending_count": int(pending_jobs["pending_jobs"]) if pending_jobs else 0,
            "reserved_bytes": (
                (int(totals["input_bytes"]) if totals else 0)
                + (int(totals["output_bytes"]) if totals else 0)
                + (int(totals["reserved_output_bytes"]) if totals else 0)
            ),
        }

    def list_records(self) -> list[dict[str, Any]]:
        rows = self._fetch(
            """
            SELECT
                job_id, state, input_path, output_dir, created_at, started_at,
                finished_at, input_expires_at, output_expires_at, tombstone_expires_at,
                input_size_bytes AS input_bytes,
                output_size_bytes AS output_bytes,
                reserved_output_bytes,
                tombstoned_at,
                error,
                exit_code
            FROM jobs
            """
        )
        records: list[dict[str, Any]] = []
        for row in rows:
            item = self._to_dict(row)
            item["output_path"] = item.get("output_dir")
            item["tombstone_path"] = str(
                self._path.parent / "jobs" / f"{item['job_id']}.json"
            )
            item["input_bytes"] = _coerce_int(item.get("input_bytes"))
            item["output_bytes"] = _coerce_int(item.get("output_bytes"))
            item["reserved_output_bytes"] = _coerce_int(item.get("reserved_output_bytes"))
            records.append(item)
        return records

    def has_active_download(self, job_id: str) -> bool:
        if not _is_uuid(job_id):
            return False
        now = _utc_now()
        row = self._fetch_one(
            """
            SELECT 1
            FROM download_leases
            WHERE job_id = ?
              AND released_at IS NULL
              AND datetime(expires_at) > datetime(?)
            LIMIT 1
            """,
            (job_id, now),
        )
        return bool(row)

    # --------------------
    # Legacy mirror sync queue
    # --------------------
    def import_legacy_state_jobs(self, state_root: str | Path) -> dict[str, Any]:
        root = Path(state_root).resolve()
        jobs_dir = root / "jobs"
        if not jobs_dir.is_dir():
            return {"imported": [], "skipped": []}

        imported: list[str] = []
        skipped: list[dict[str, str]] = []
        now = _utc_now()

        for path in sorted(jobs_dir.glob("*.json")):
            try:
                raw = path.read_text(encoding="utf-8")
                payload = json.loads(raw)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                skipped.append({"path": str(path), "reason": "invalid_json"})
                continue

            if not isinstance(payload, dict):
                skipped.append({"path": str(path), "reason": "invalid_payload"})
                continue

            keys = set(payload.keys())
            if set(self.LEGACY_FIELDS) != keys:
                skipped.append({"path": str(path), "reason": "invalid_fields"})
                continue

            if payload.get("state") not in self.VALID_STATES:
                skipped.append({"path": str(path), "reason": "invalid_state"})
                continue

            job_id = str(payload.get("job_id"))
            if not _is_uuid(job_id):
                skipped.append({"path": str(path), "reason": "invalid_uuid"})
                continue

            required = set(self.LEGACY_FIELDS)
            if not required.issubset(payload):
                skipped.append({"path": str(path), "reason": "missing_fields"})
                continue

            try:
                input_path = str(payload["input_path"])
                output_dir = str(payload["output_dir"])
                self._validate_rooted_path(input_path, self._input_root, "input_path")
                self._validate_rooted_path(output_dir, self._output_root, "output_dir")
            except ValueError:
                skipped.append({"path": str(path), "reason": "invalid_root_path"})
                continue

            if self.get_job(job_id):
                skipped.append({"path": str(path), "reason": "already_imported"})
                continue

            source_payload = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            source_hash = _sha256_text(source_payload)

            with self._write_txn():
                try:
                    self._conn.execute(
                        """
                        INSERT INTO jobs (
                            job_id, state, original_name, client_reference, input_path,
                            output_dir, input_sha256, input_size_bytes, output_size_bytes,
                            manifest_version, input_expires_at, output_expires_at,
                            tombstone_expires_at, tombstoned_at, tombstone_reason,
                            reserved_output_bytes, input_deleted_at, output_deleted_at, deleted,
                            delete_requested_at, deleted_at,
                            created_at, started_at, finished_at, exit_code, error, updated_at
                        )
                        VALUES (?, ?, ?, NULL, ?, ?, NULL, 0, 0, 0, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0, NULL, NULL,
                                ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            str(payload.get("state")),
                            str(payload.get("original_name")),
                            str(payload.get("input_path")),
                            str(payload.get("output_dir")),
                            payload.get("created_at"),
                            payload.get("started_at"),
                            payload.get("finished_at"),
                            (
                                _coerce_int(payload.get("exit_code"))
                                if payload.get("exit_code") is not None
                                else None
                            ),
                            payload.get("error"),
                            _utc_now(),
                        ),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO legacy_sync (
                            job_id, source_path, source_hash, imported_at, status,
                            attempts, max_attempts, next_attempt_at, updated_at,
                            legacy_payload
                        )
                        VALUES (?, ?, ?, ?, 'pending', 0, 6, ?, ?, ?)
                        """,
                        (
                            job_id,
                            str(path),
                            source_hash,
                            now,
                            now,
                            now,
                            source_payload,
                        ),
                    )
                except sqlite3.IntegrityError:
                    skipped.append({"path": str(path), "reason": "conflict"})
                    continue
            imported.append(job_id)

        return {"imported": imported, "skipped": skipped}

    def legacy_record(self, job_id: str) -> dict[str, Any]:
        """Return the strict ten-field v1.0.2 rollback mirror."""

        record = self.get_job(job_id)
        if not record:
            return {}
        return {field: record.get(field) for field in self.LEGACY_FIELDS}

    def list_legacy_sync_queue(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        criteria: list[str] = []
        params: list[Any] = []
        if status:
            criteria.append("status = ?")
            params.append(status)
        if cursor:
            try:
                decoded = _from_cursor(cursor)
            except Exception:
                return {"error": "invalid_cursor"}
            marker_next = decoded.get("next_attempt_at")
            marker_id = decoded.get("id")
            if not marker_next or marker_id is None:
                return {"error": "invalid_cursor"}
            criteria.append("(next_attempt_at < ? OR (next_attempt_at = ? AND id < ?))")
            params.extend([marker_next, marker_next, marker_id])

        where = f"WHERE {' AND '.join(criteria)}" if criteria else ""
        rows = self._fetch(
            f"""
            SELECT * FROM legacy_sync
            {where}
            ORDER BY next_attempt_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params + [limit + 1]),
        )

        items = []
        for row in rows[:limit]:
            item = self._to_dict(row)
            payload = _coerce_json(item.pop("legacy_payload"))
            item["legacy_payload"] = payload
            items.append(item)

        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = _to_cursor(
                {"next_attempt_at": last["next_attempt_at"], "id": last["id"]}
            )

        return {"items": items, "count": len(items), "next_cursor": next_cursor}

    def claim_legacy_sync_job(self, *, worker_id: str | None = None, lease_seconds: int = 60) -> dict[str, Any]:
        now = _utc_now()
        lease = (datetime.fromisoformat(now) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._write_txn():
            row = self._fetch_one(
                """
                SELECT id, job_id, attempts, max_attempts, next_attempt_at
                FROM legacy_sync
                WHERE status IN ('pending', 'retrying')
                  AND datetime(next_attempt_at) <= datetime(?)
                  AND (locked_until IS NULL OR datetime(locked_until) <= datetime(?))
                  AND (attempts < max_attempts)
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT 1
                """,
                (now, now),
            )
            if not row:
                return {}
            sync_id = int(row["id"])
            new_attempts = int(row["attempts"]) + 1
            self._conn.execute(
                """
                UPDATE legacy_sync
                SET status = 'in_progress',
                    attempts = ?,
                    locked_until = ?,
                    locked_by = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_attempts, lease, worker_id, now, sync_id),
            )
            return self.get_legacy_sync(sync_id)

    def complete_legacy_sync_job(
        self,
        sync_id: int,
        *,
        success: bool,
        error: str | None = None,
    ) -> dict[str, Any]:
        row = self._to_dict(self._fetch_one("SELECT * FROM legacy_sync WHERE id = ?", (sync_id,)))
        if not row:
            return {"error": "legacy_job_not_found"}

        now = _utc_now()
        status = "completed" if success else "failed"
        with self._write_txn():
            self._conn.execute(
                """
                UPDATE legacy_sync
                SET status = ?,
                    completed_at = ?,
                    locked_until = NULL,
                    locked_by = NULL,
                    last_error = ?,
                    last_synced_at = ?
                WHERE id = ?
                """,
                (status, now, error, now if success else None, sync_id),
            )
        return {"id": sync_id, "status": status}

    def get_legacy_sync(self, sync_id: int) -> dict[str, Any]:
        row = self._to_dict(self._fetch_one("SELECT * FROM legacy_sync WHERE id = ?", (sync_id,)))
        if not row:
            return {}
        payload = _coerce_json(row.pop("legacy_payload"))
        row["legacy_payload"] = payload
        return row

    # --------------------
    # Idempotency
    # --------------------
    def register_idempotency_key(
        self,
        key: str,
        job_id: str,
        *,
        request_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if not key or not _is_uuid(job_id):
            return {"error": "invalid_input"}

        if not self.get_job(job_id):
            return {"error": "job_not_found"}

        now = _utc_now()
        expires = (datetime.fromisoformat(now) + timedelta(seconds=self.DEFAULT_IDEMPOTENCY_TTL_SECONDS)).isoformat()

        with self._write_txn():
            row = self._to_dict(
                self._fetch_one(
                    "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
                    (key,),
                )
            )
            if row:
                expires_at = _parse_datetime(row.get("expires_at"))
                if expires_at is not None and expires_at <= datetime.now(timezone.utc):
                    self._conn.execute(
                        "DELETE FROM idempotency_keys WHERE idempotency_key = ?",
                        (key,),
                    )
                    row = {}
            if row:
                if row.get("request_fingerprint") != request_fingerprint:
                    return {"error": "idempotency_conflict"}
                return row
            try:
                self._conn.execute(
                    """
                    INSERT INTO idempotency_keys (
                        idempotency_key, job_id, created_at, request_fingerprint, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, job_id, now, request_fingerprint, expires),
                )
            except sqlite3.IntegrityError:
                return {"error": "idempotency_conflict"}
            return self._to_dict(
                self._fetch_one(
                    "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
                    (key,),
                )
            )

    def resolve_idempotency_key(self, key: str) -> dict[str, Any]:
        row = self._to_dict(
            self._fetch_one(
                "SELECT * FROM idempotency_keys WHERE idempotency_key = ?", (key,)
            )
        )
        expires_at = _parse_datetime(row.get("expires_at")) if row else None
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            with self._write_txn():
                self._conn.execute(
                    "DELETE FROM idempotency_keys WHERE idempotency_key = ?", (key,)
                )
            return {}
        return row

    def purge_expired_idempotency_keys(self) -> int:
        with self._write_txn():
            result = self._conn.execute(
                "DELETE FROM idempotency_keys WHERE datetime(expires_at) <= datetime(?)",
                (_utc_now(),),
            )
        return int(result.rowcount)

    # --------------------
    # Download lease
    # --------------------
    def acquire_download_lease(
        self,
        job_id: str,
        relative_path: str,
        *,
        holder: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        if not self.get_job(job_id):
            return {"error": "job_not_found"}
        relative = PurePosixPath(relative_path)
        if (
            not relative_path
            or "\\" in relative_path
            or "\x00" in relative_path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            return {"error": "invalid_relative_path"}

        now = datetime.fromisoformat(_utc_now())
        lease_id = str(uuid4())
        expires = now + timedelta(seconds=max(1, ttl_seconds))
        now_iso = now.isoformat()
        expires_iso = expires.isoformat()

        with self._write_txn():
            job = self._to_dict(
                self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            )
            if not job:
                return {"error": "job_not_found"}
            if job.get("output_deleted_at") is not None or bool(job.get("deleted")):
                return {"error": "output_expired"}
            cleanup = self._fetch_one(
                """
                SELECT 1 FROM cleanup_claims
                WHERE job_id = ? AND kind = 'output'
                  AND completed_at IS NULL
                  AND datetime(expires_at) > datetime(?)
                """,
                (job_id, now_iso),
            )
            if cleanup:
                return {"error": "cleanup_in_progress"}
            if relative_path == "__archive__":
                if job.get("state") not in self.TERMINAL_STATES:
                    return {"error": "job_not_terminal"}
            else:
                published = self._fetch_one(
                    """
                    SELECT 1 FROM job_files
                    WHERE job_id = ? AND path = ? AND status != 'deleted'
                    """,
                    (job_id, relative_path),
                )
                if not published:
                    return {"error": "file_not_published"}

            existing = self._to_dict(
                self._fetch_one(
                    """
                    SELECT * FROM download_leases
                    WHERE job_id = ? AND relative_path = ?
                    """,
                    (job_id, relative_path),
                )
            )
            if existing:
                released = existing.get("released_at")
                if released is None:
                    if _parse_datetime(existing["expires_at"]) and _parse_datetime(existing["expires_at"]) > now:
                        if holder is None or holder == existing.get("holder"):
                            return existing
                        return {"error": "lease_conflict"}
                self._conn.execute(
                    "DELETE FROM download_leases WHERE lease_id = ?",
                    (existing["lease_id"],),
                )

            self._conn.execute(
                """
                INSERT INTO download_leases (
                    lease_id, job_id, relative_path, holder,
                    created_at, updated_at, expires_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (lease_id, job_id, relative_path, holder, now_iso, now_iso, expires_iso),
            )

            return self._to_dict(
                self._fetch_one(
                    "SELECT * FROM download_leases WHERE lease_id = ?",
                    (lease_id,),
                )
            )

    def renew_download_lease(self, lease_id: str, *, ttl_seconds: int = 300) -> dict[str, Any]:
        row = self._to_dict(self._fetch_one("SELECT * FROM download_leases WHERE lease_id = ?", (lease_id,)))
        if not row:
            return {"error": "lease_not_found"}
        if row.get("released_at") is not None:
            return {"error": "lease_released"}
        expires_at = _parse_datetime(row.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return {"error": "lease_expired"}

        now = datetime.fromisoformat(_utc_now())
        expires = now + timedelta(seconds=max(1, ttl_seconds))
        with self._write_txn():
            self._conn.execute(
                """
                UPDATE download_leases
                SET updated_at = ?, expires_at = ?
                WHERE lease_id = ?
                """,
                (now.isoformat(), expires.isoformat(), lease_id),
            )
        return self._to_dict(self._fetch_one("SELECT * FROM download_leases WHERE lease_id = ?", (lease_id,)))

    def release_download_lease(self, lease_id: str) -> dict[str, Any]:
        row = self._to_dict(self._fetch_one("SELECT * FROM download_leases WHERE lease_id = ?", (lease_id,)))
        if not row:
            return {"error": "lease_not_found"}
        if row.get("released_at") is not None:
            return row

        now = _utc_now()
        with self._write_txn():
            self._conn.execute(
                """
                UPDATE download_leases
                SET released_at = ?, updated_at = ?
                WHERE lease_id = ?
                """,
                (now, now, lease_id),
            )
        return self._to_dict(self._fetch_one("SELECT * FROM download_leases WHERE lease_id = ?", (lease_id,)))

    def list_expired_download_leases(self, *, now: datetime | None = None) -> dict[str, Any]:
        if now is None:
            now = datetime.now(timezone.utc)
        rows = self._fetch(
            """
            SELECT *
            FROM download_leases
            WHERE released_at IS NULL
              AND datetime(expires_at) <= datetime(?)
            ORDER BY expires_at ASC
            """,
            (now.isoformat(),),
        )
        return {"items": [self._to_dict(row) for row in rows], "count": len(rows)}

    def purge_expired_download_leases(self, *, now: datetime | None = None) -> int:
        instant = (now or datetime.now(timezone.utc)).isoformat()
        with self._write_txn():
            result = self._conn.execute(
                "DELETE FROM download_leases WHERE datetime(expires_at) <= datetime(?)",
                (instant,),
            )
        return int(result.rowcount)

    def list_expired_job_candidates(
        self,
        *,
        terminal_states: set[str] | None = None,
        max_age_seconds: int = 3600,
    ) -> dict[str, Any]:
        states = terminal_states or self.TERMINAL_STATES
        values = tuple(sorted(states))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        rows = self._fetch(
            f"""
            SELECT * FROM jobs
            WHERE state IN {_build_placeholders(values)}
              AND tombstoned_at IS NULL
              AND finished_at IS NOT NULL
              AND datetime(finished_at) <= datetime(?)
            ORDER BY finished_at ASC
            """,
            tuple(values) + (cutoff,),
        )
        return {"items": [self._row_to_job(row) for row in rows], "count": len(rows)}

    def tombstone_jobs(self, job_ids: Sequence[str], *, reason: str | None = None) -> dict[str, Any]:
        ids = [str(job_id) for job_id in job_ids if _is_uuid(str(job_id))]
        if not ids:
            return {"error": "empty_job_ids", "updated": 0}

        placeholder = ", ".join("?" for _ in ids)
        now = _utc_now()
        with self._write_txn():
            self._conn.execute(
                f"""
                UPDATE jobs
                SET tombstoned_at = ?, tombstone_reason = ?
                WHERE job_id IN ({placeholder}) AND tombstoned_at IS NULL
                """,
                (now, reason, *ids),
            )
            row = self._fetch_one("SELECT changes() AS updated")
        return {"updated": int(row["updated"]), "job_ids": ids}

    # --------------------
    # Webhooks
    # --------------------
    def create_webhook_subscription(
        self,
        *,
        callback_url: str,
        event_types: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        secret: str | None = None,
        headers: Mapping[str, Any] | None = None,
        name: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if not callback_url:
            return {"error": "invalid_callback_url"}
        now = _utc_now()
        with self._write_txn():
            if self._max_webhook_subscriptions is not None:
                current = self._fetch_one(
                    "SELECT COUNT(*) AS count FROM webhook_subscriptions"
                )
                if int(current["count"]) >= self._max_webhook_subscriptions:
                    return {"error": "subscription_limit"}
            cur = self._conn.execute(
                """
                INSERT INTO webhook_subscriptions (
                    callback_url, event_types, filters, secret, headers,
                    enabled, name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    callback_url,
                    json.dumps(list(event_types or []), ensure_ascii=False),
                    json.dumps(dict(filters or {}), ensure_ascii=False),
                    secret,
                    json.dumps(dict(headers or {}), ensure_ascii=False),
                    _coerce_bool_to_int(enabled),
                    name,
                    now,
                    now,
                ),
            )
            subscription_id = int(cur.lastrowid)
        return self.get_webhook_subscription(subscription_id)

    def get_webhook_subscription(self, subscription_id: int) -> dict[str, Any]:
        row = self._to_dict(
            self._fetch_one("SELECT * FROM webhook_subscriptions WHERE id = ?", (subscription_id,))
        )
        if not row:
            return {}
        row["event_types"] = _coerce_json(row.get("event_types")) or []
        row["filters"] = _coerce_json(row.get("filters")) or {}
        row["headers"] = _coerce_json(row.get("headers")) or {}
        row["enabled"] = bool(_coerce_int(row.get("enabled")))
        return row

    def list_webhook_subscriptions(self, *, include_disabled: bool = False) -> dict[str, Any]:
        if include_disabled:
            rows = self._fetch("SELECT * FROM webhook_subscriptions ORDER BY id ASC")
        else:
            rows = self._fetch(
                "SELECT * FROM webhook_subscriptions WHERE enabled = 1 ORDER BY id ASC"
            )
        items: list[dict[str, Any]] = []
        for row in rows:
            parsed = self._to_dict(row)
            parsed["event_types"] = _coerce_json(parsed.get("event_types")) or []
            parsed["filters"] = _coerce_json(parsed.get("filters")) or {}
            parsed["headers"] = _coerce_json(parsed.get("headers")) or {}
            parsed["enabled"] = bool(_coerce_int(parsed.get("enabled")))
            items.append(parsed)
        return {"items": items, "count": len(items)}

    def update_webhook_subscription(
        self,
        subscription_id: int,
        *,
        callback_url: str | None = None,
        event_types: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        secret: str | None = None,
        headers: Mapping[str, Any] | None = None,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if callback_url is not None:
            if not callback_url:
                return {"error": "invalid_callback_url"}
            updates["callback_url"] = callback_url
        if event_types is not None:
            updates["event_types"] = json.dumps(list(event_types), ensure_ascii=False)
        if filters is not None:
            updates["filters"] = json.dumps(dict(filters), ensure_ascii=False)
        if secret is not None:
            updates["secret"] = secret
        if headers is not None:
            updates["headers"] = json.dumps(dict(headers), ensure_ascii=False)
        if name is not None:
            updates["name"] = name
        if enabled is not None:
            updates["enabled"] = _coerce_bool_to_int(enabled)

        if not updates:
            return self.get_webhook_subscription(subscription_id)

        updates["updated_at"] = _utc_now()
        set_sql = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values())
        params.append(subscription_id)

        with self._write_txn():
            result = self._conn.execute(
                f"UPDATE webhook_subscriptions SET {set_sql} WHERE id = ?",
                params,
            )
            if result.rowcount == 0:
                return {"error": "subscription_not_found"}
        return self.get_webhook_subscription(subscription_id)

    def delete_webhook_subscription(self, subscription_id: int) -> dict[str, Any]:
        row = self.get_webhook_subscription(subscription_id)
        if not row:
            return {"error": "subscription_not_found"}
        with self._write_txn():
            self._conn.execute("DELETE FROM webhook_subscriptions WHERE id = ?", (subscription_id,))
        return {"deleted": True, "subscription_id": subscription_id}

    def _subscription_matches(
        self,
        subscription: dict[str, Any],
        event_type: str,
        payload: Mapping[str, Any],
    ) -> bool:
        events = subscription.get("event_types") or []
        if events and event_type not in events:
            return False
        filters = subscription.get("filters") or {}
        if not isinstance(filters, Mapping):
            return False
        for key, expected in filters.items():
            if payload.get(key) != expected:
                return False
        return True

    def enqueue_webhook_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not event_type:
            return {"error": "invalid_event_type"}
        if not isinstance(payload, Mapping):
            return {"error": "invalid_payload"}

        now_iso = (now or datetime.now(timezone.utc)).isoformat()
        job_id = payload.get("job_id")
        job_id = job_id if isinstance(job_id, str) and _is_uuid(job_id) else None
        serialized = json.dumps(payload, ensure_ascii=False)
        payload_text = self._coerce_payload(serialized) or {}

        created: list[dict[str, Any]] = []
        with self._write_txn():
            subs = self._fetch("SELECT * FROM webhook_subscriptions WHERE enabled = 1")
            event_id = str(
                uuid5(
                    NAMESPACE_URL,
                    "docling:outbox:"
                    + event_type
                    + ":"
                    + json.dumps(payload_text, ensure_ascii=False, sort_keys=True),
                )
            )
            for row in subs:
                parsed = self.get_webhook_subscription(int(row["id"]))
                if not self._subscription_matches(parsed, event_type, payload_text):
                    continue
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO webhook_deliveries (
                        subscription_id, job_id, event_type, event_id, payload,
                        status, max_attempts, attempts, next_attempt_at,
                        locked_until, locked_by, created_at, updated_at,
                        last_error, last_status_code, last_response
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, NULL, NULL, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        int(row["id"]),
                        job_id,
                        event_type,
                        event_id,
                        json.dumps(payload_text, ensure_ascii=False),
                        self._webhook_max_attempts,
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )
                if cur.rowcount:
                    created.append({"id": int(cur.lastrowid), "event_id": event_id, "subscription_id": int(row["id"])})

        deliveries = self.list_webhook_deliveries(delivery_ids=[entry["id"] for entry in created])
        return {"created": deliveries.get("items", []), "count": len(created)}

    def list_webhook_deliveries(
        self,
        *,
        subscription_id: int | None = None,
        status: str | None = None,
        job_id: str | None = None,
        delivery_ids: Sequence[int] | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if limit < 1:
            limit = 1
        if status and status not in self.WEBHOOK_DELIVERY_STATUSES:
            return {"error": "invalid_status"}

        criteria: list[str] = []
        params: list[Any] = []

        if subscription_id is not None:
            criteria.append("d.subscription_id = ?")
            params.append(subscription_id)
        if status is not None:
            criteria.append("d.status = ?")
            params.append(status)
        if job_id:
            criteria.append("d.job_id = ?")
            params.append(job_id)
        if delivery_ids:
            if len(delivery_ids) > 1000:
                return {"error": "too_many_ids"}
            placeholder = ", ".join("?" for _ in delivery_ids)
            criteria.append(f"d.id IN ({placeholder})")
            params.extend(delivery_ids)

        if cursor:
            try:
                decoded = _from_cursor(cursor)
            except Exception:
                return {"error": "invalid_cursor"}
            marker_created = decoded.get("created_at")
            marker_id = decoded.get("id")
            if not marker_created or marker_id is None:
                return {"error": "invalid_cursor"}
            if (
                decoded.get("subscription_id") != subscription_id
                or decoded.get("status") != status
                or decoded.get("job_id") != job_id
            ):
                return {"error": "invalid_cursor"}
            criteria.append("(d.created_at < ? OR (d.created_at = ? AND d.id < ?))")
            params.extend([marker_created, marker_created, marker_id])

        where = f"WHERE {' AND '.join(criteria)}" if criteria else ""
        rows = self._fetch(
            f"""
            SELECT
                d.id, d.subscription_id, d.job_id, d.event_type, d.event_id,
                d.payload, d.status, d.max_attempts, d.attempts,
                d.next_attempt_at, d.locked_until, d.locked_by, d.created_at,
                d.updated_at, d.last_error, d.last_status_code, d.last_response,
                s.callback_url, s.secret, s.headers
            FROM webhook_deliveries AS d
            JOIN webhook_subscriptions AS s
              ON s.id = d.subscription_id
            {where}
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT ?
            """,
            tuple(params + [limit + 1]),
        )

        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            item = self._to_dict(row)
            item["payload"] = _coerce_json(item.get("payload")) or {}
            item["headers"] = _coerce_json(item.get("headers")) or {}
            item["secret"] = item.get("secret")
            item["attempts"] = _coerce_int(item.get("attempts"))
            item["max_attempts"] = _coerce_int(item.get("max_attempts"),) if item.get("max_attempts") is not None else self._webhook_max_attempts
            items.append(item)

        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = _to_cursor(
                {
                    "created_at": last["created_at"],
                    "id": last["id"],
                    "subscription_id": subscription_id,
                    "status": status,
                    "job_id": job_id,
                }
            )

        return {"items": items, "count": len(items), "next_cursor": next_cursor}

    def claim_webhook_delivery(
        self,
        now: datetime | None = None,
        lease_seconds: int = 60,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).isoformat()
        until = (datetime.fromisoformat(current) + timedelta(seconds=max(1, lease_seconds))).isoformat()

        with self._write_txn():
            self._conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = CASE
                        WHEN attempts >= max_attempts THEN 'failed'
                        ELSE 'retrying'
                    END,
                    locked_until = NULL,
                    locked_by = NULL,
                    next_attempt_at = ?,
                    updated_at = ?,
                    last_error = COALESCE(last_error, 'delivery lease expired')
                WHERE status = 'in_progress'
                  AND locked_until IS NOT NULL
                  AND datetime(locked_until) <= datetime(?)
                """,
                (current, current, current),
            )
            row = self._fetch_one(
                """
                SELECT d.id, d.job_id, d.event_type, d.event_id, d.payload, d.status,
                       d.max_attempts, d.attempts, d.next_attempt_at, d.locked_until,
                       s.callback_url, s.secret, s.headers
                FROM webhook_deliveries AS d
                JOIN webhook_subscriptions AS s
                  ON s.id = d.subscription_id
                WHERE d.status IN ('pending', 'retrying')
                  AND datetime(d.next_attempt_at) <= datetime(?)
                  AND (d.locked_until IS NULL OR datetime(d.locked_until) <= datetime(?))
                  AND d.attempts < d.max_attempts
                ORDER BY d.next_attempt_at ASC, d.id ASC
                LIMIT 1
                """,
                (current, current),
            )

            if not row:
                return {}

            delivery_id = int(row["id"])
            self._conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'in_progress',
                    attempts = attempts + 1,
                    locked_until = ?,
                    locked_by = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (until, worker_id, current, delivery_id),
            )
            claimed = self._to_dict(
                self._fetch_one(
                    """
                    SELECT d.id, d.job_id, d.event_type, d.event_id, d.payload, d.status,
                           d.max_attempts, d.attempts, d.next_attempt_at, d.locked_until, d.locked_by,
                           d.created_at, d.updated_at,
                           s.callback_url, s.secret, s.headers
                    FROM webhook_deliveries AS d
                    JOIN webhook_subscriptions AS s
                      ON s.id = d.subscription_id
                    WHERE d.id = ?
                    """,
                    (delivery_id,),
                )
            )

        if not claimed:
            return {}

        claimed["payload"] = _coerce_json(claimed.get("payload")) or {}
        claimed["headers"] = _coerce_json(claimed.get("headers")) or {}
        claimed["status"] = "in_progress"
        claimed["id"] = delivery_id
        claimed["attempts"] = _coerce_int(claimed.get("attempts"))
        claimed["max_attempts"] = _coerce_int(claimed.get("max_attempts"), self._webhook_max_attempts)
        return claimed

    def complete_webhook_delivery(
        self,
        delivery_id: int,
        *,
        status: str | None = None,
        success: bool | None = None,
        status_code: int | None = None,
        response: str | None = None,
        error: str | None = None,
        next_attempt_at: str | datetime | None = None,
        attempts: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        if status is None:
            if success is None:
                return {"error": "invalid_status"}
            status = "succeeded" if success else "failed"

        if status not in {"succeeded", "failed", "retrying"}:
            return {"error": "invalid_status"}

        next_iso = None
        if next_attempt_at is not None:
            if isinstance(next_attempt_at, datetime):
                next_iso = next_attempt_at.isoformat()
            else:
                next_iso = str(next_attempt_at)
        elif status == "retrying":
            next_iso = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()

        with self._write_txn():
            row = self._to_dict(
                self._fetch_one(
                    "SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)
                )
            )
            if not row:
                return {"error": "delivery_not_found"}
            now = _utc_now()
            if row.get("status") == "in_progress":
                locked_until = _parse_datetime(row.get("locked_until"))
                if (
                    worker_id is None
                    or row.get("locked_by") != worker_id
                    or locked_until is None
                    or locked_until <= datetime.now(timezone.utc)
                ):
                    return {"error": "delivery_lease_conflict"}
            elif worker_id is not None:
                return {"error": "delivery_lease_conflict"}

            effective_attempts = _coerce_int(
                attempts if attempts is not None else row.get("attempts")
            )
            max_attempts = _coerce_int(
                row.get("max_attempts"), self._webhook_max_attempts
            )
            effective_status = status
            if effective_status == "retrying" and effective_attempts >= max_attempts:
                effective_status = "failed"
            next_lock = next_iso if effective_status == "retrying" else None
            if effective_status == "retrying" and error is None:
                error = "retry"

            criteria = "id = ?"
            criteria_values: list[Any] = [delivery_id]
            if worker_id is not None:
                criteria += (
                    " AND status = 'in_progress' AND locked_by = ?"
                    " AND datetime(locked_until) > datetime(?)"
                )
                criteria_values.extend([worker_id, now])
            else:
                criteria += " AND status != 'in_progress'"

            result = self._conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = ?,
                    attempts = ?,
                    last_status_code = ?,
                    last_response = ?,
                    last_error = ?,
                    next_attempt_at = COALESCE(?, next_attempt_at),
                    updated_at = ?,
                    locked_by = ?,
                    locked_until = ?
                WHERE """ + criteria,
                (
                    effective_status,
                    effective_attempts,
                    status_code,
                    response,
                    error,
                    next_iso,
                    now,
                    None,
                    next_lock,
                    *criteria_values,
                ),
            )
            if result.rowcount != 1:
                return {"error": "delivery_lease_conflict"}

            return self.get_webhook_delivery(delivery_id)

    def retry_webhook_delivery(
        self,
        delivery_id: int,
        *,
        error: str,
        retry_after_seconds: int = 60,
    ) -> dict[str, Any]:
        row = self._to_dict(self._fetch_one("SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)))
        if not row:
            return {"error": "delivery_not_found"}
        if _coerce_int(row.get("attempts", 0)) >= _coerce_int(row.get("max_attempts", self._webhook_max_attempts)):
            return {"error": "max_attempts_reached"}

        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=max(1, retry_after_seconds))
        return self.complete_webhook_delivery(
            delivery_id,
            status="retrying",
            status_code=_coerce_int(row.get("last_status_code")),
            error=error,
            next_attempt_at=next_attempt.isoformat(),
            attempts=_coerce_int(row.get("attempts")),
        )

    def purge_webhook_deliveries(
        self,
        *,
        max_age_seconds: int = 7 * 24 * 60 * 60,
        statuses: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, max_age_seconds))).isoformat()
        purge_status = statuses or tuple(sorted(self.WEBHOOK_DELIVERY_STATUSES))
        if not purge_status:
            return {"error": "invalid_statuses"}

        placeholder = ", ".join("?" for _ in purge_status)
        with self._write_txn():
            row = self._conn.execute(
                f"""
                DELETE FROM webhook_deliveries
                WHERE status IN ({placeholder})
                  AND datetime(created_at) <= datetime(?)
                  AND NOT (
                    status = 'in_progress'
                    AND locked_until IS NOT NULL
                    AND datetime(locked_until) > datetime(?)
                  )
                """,
                tuple(purge_status) + (cutoff, _utc_now()),
            )
        return {"deleted": int(row.rowcount)}

    def get_webhook_delivery(self, delivery_id: int) -> dict[str, Any]:
        row = self._to_dict(self._fetch_one("SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)))
        if not row:
            return {}
        row["payload"] = _coerce_json(row.get("payload")) or {}
        return row

    # --------------------
    # Lifecycle hooks
    # --------------------
    def claim_cleanup(self, job_id: str, kind: str, now: float) -> str | None:
        if not job_id or len(job_id) > 255 or not kind or len(kind) > 64:
            return None
        claimed_at = datetime.fromtimestamp(now, tz=timezone.utc)
        lease_until = claimed_at + timedelta(seconds=self.DEFAULT_CLEANUP_LEASE_SECONDS)
        claim_id = str(uuid4())

        with self._write_txn():
            if kind == "output":
                active_download = self._fetch_one(
                    """
                    SELECT 1 FROM download_leases
                    WHERE job_id = ? AND released_at IS NULL
                      AND datetime(expires_at) > datetime(?)
                    LIMIT 1
                    """,
                    (job_id, claimed_at.isoformat()),
                )
                if active_download:
                    return None
            row = self._to_dict(
                self._fetch_one(
                    "SELECT * FROM cleanup_claims WHERE job_id = ? AND kind = ?",
                    (job_id, kind),
                )
            )

            if row:
                if row.get("completed_at") is not None:
                    return None
                if _parse_datetime(row.get("expires_at", "")) and _parse_datetime(row.get("expires_at")) > claimed_at:
                    return None

            self._conn.execute(
                """
                INSERT INTO cleanup_claims (
                    job_id, kind, lease_id, claimed_at, expires_at,
                    completed_at, last_error, last_deleted_bytes
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)
                ON CONFLICT(job_id, kind)
                DO UPDATE SET
                    lease_id = excluded.lease_id,
                    claimed_at = excluded.claimed_at,
                    expires_at = excluded.expires_at,
                    completed_at = NULL,
                    last_error = NULL,
                    last_deleted_bytes = 0
                """,
                (
                    job_id,
                    kind,
                    claim_id,
                    claimed_at.isoformat(),
                    lease_until.isoformat(),
                ),
            )

        return claim_id

    def complete_cleanup(
        self,
        job_id: str,
        kind: str,
        *,
        lease_id: str,
        deleted_bytes: int,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._write_txn():
            claim = self._fetch_one(
                """
                SELECT * FROM cleanup_claims
                WHERE job_id = ? AND kind = ? AND lease_id = ?
                  AND datetime(expires_at) > datetime(?)
                """,
                (job_id, kind, lease_id, now),
            )
            if not claim:
                return {"error": "cleanup_lease_conflict"}

            self._conn.execute(
                """
                UPDATE cleanup_claims
                SET completed_at = ?,
                    last_error = ?,
                    last_deleted_bytes = ?,
                    expires_at = CASE WHEN ? IS NULL THEN expires_at ELSE ? END
                WHERE job_id = ? AND kind = ? AND lease_id = ?
                """,
                (
                    now if error is None else None,
                    error,
                    max(0, int(deleted_bytes)),
                    error,
                    now,
                    job_id,
                    kind,
                    lease_id,
                ),
            )

            if error is None and kind == "input":
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET input_deleted_at = ?,
                        input_size_bytes = 0,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, now, job_id),
                )
            elif error is None and kind == "output":
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET output_deleted_at = ?,
                        output_size_bytes = 0,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, now, job_id),
                )
                self._conn.execute(
                    "UPDATE job_files SET status = 'deleted', deleted_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
            elif error is None and kind in {
                "staging_dir",
                "temp_dir",
                "tombstone_dir",
                "orphan_input",
            }:
                self._conn.execute(
                    """
                    DELETE FROM cleanup_claims
                    WHERE job_id = ? AND kind = ? AND lease_id = ?
                    """,
                    (job_id, kind, lease_id),
                )

            if error is None and kind == "tombstone":
                self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                self._conn.execute(
                    "DELETE FROM cleanup_claims WHERE job_id = ?",
                    (job_id,),
                )

        return {
            "job_id": job_id,
            "kind": kind,
            "deleted_bytes": max(0, deleted_bytes),
            "error": error,
        }

    def hard_delete_job(self, job_id: str) -> bool:
        if not _is_uuid(job_id):
            return False
        with self._write_txn():
            result = self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM cleanup_claims WHERE job_id = ?", (job_id,))
        return result.rowcount > 0

    def finalize_job(
        self,
        job_id: str,
        *,
        state: str,
        manifest: Sequence[Mapping[str, Any]] | None = None,
        exit_code: int | None = None,
        error: str | None = None,
        finished_at: str | None = None,
        manifest_version: int | None = None,
        webhook_event_type: str = "job.completed",
        webhook_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in self.TERMINAL_STATES:
            return {"error": "invalid_terminal_state"}

        now = _utc_now()
        normalized_manifest = list(manifest or [])
        manifest_json = json.dumps(
            normalized_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

        try:
            with self._write_txn():
                record = self._row_to_job(
                    self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
                )
                if not record:
                    return {"error": "job_not_found"}
                if record.get("state") in self.TERMINAL_STATES:
                    if record.get("state") != state:
                        return {"error": "job_terminal_conflict"}
                    deliveries = self.list_webhook_deliveries(job_id=job_id)
                    return {
                        "job": record,
                        "manifest": self.list_manifest(job_id).get("items", []),
                        "deliveries": deliveries.get("items", []),
                    }
                updates = {
                    "state": state,
                    "finished_at": finished_at or now,
                    "exit_code": exit_code,
                    "error": error,
                    "updated_at": now,
                    "manifest_version": _coerce_int(manifest_version, _coerce_int(record.get("manifest_version")) + 1),
                    "manifest_sha256": manifest_sha256,
                    "reserved_output_bytes": 0,
                }
                sets = ", ".join(f"{key} = ?" for key in updates)
                values = list(updates.values()) + [job_id]
                result = self._conn.execute(
                    f"""
                    UPDATE jobs SET {sets}
                    WHERE job_id = ? AND state IN ('queued', 'running')
                    """,
                    values,
                )
                if result.rowcount != 1:
                    return {"error": "job_state_conflict"}

                if manifest is not None:
                    self._replace_manifest_txn(job_id, manifest, now)

                # enqueue stable webhook outbox (one logical event_id per job event)
                payload = {
                    "job_id": job_id,
                    "state": state,
                }
                if webhook_payload:
                    payload.update(dict(webhook_payload))
                event_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"docling:{job_id}:{webhook_event_type}:{updates['manifest_version']}",
                    )
                )

                subscriptions = self._fetch(
                    "SELECT * FROM webhook_subscriptions WHERE enabled = 1"
                )
                payload_json = json.dumps(payload, ensure_ascii=False)
                for sub in subscriptions:
                    parsed = self.get_webhook_subscription(int(sub["id"]))
                    if not self._subscription_matches(parsed, webhook_event_type, payload):
                        continue
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO webhook_deliveries (
                            subscription_id, job_id, event_type, event_id, payload,
                            status, max_attempts, attempts, next_attempt_at,
                            locked_until, locked_by, created_at, updated_at,
                            last_error, last_status_code, last_response
                        )
                        VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, NULL, NULL, ?, ?, NULL, NULL, NULL)
                        """,
                        (
                            int(sub["id"]),
                            job_id,
                            webhook_event_type,
                            event_id,
                            payload_json,
                            self._webhook_max_attempts,
                            now,
                            now,
                            now,
                        ),
                    )
        except ValueError as exc:
            return {"error": str(exc)}

        finalized = self.get_job(job_id)
        deliveries = self.list_webhook_deliveries(job_id=job_id)
        return {"job": finalized, "manifest": manifest, "deliveries": deliveries.get("items", [])}
