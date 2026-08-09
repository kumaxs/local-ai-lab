from __future__ import annotations

import os
import tempfile
import threading
import uuid
import unittest
from pathlib import Path
from time import sleep

from docling_service.lifecycle import (
    CLEANUP_KIND_INPUT,
    CLEANUP_KIND_OUTPUT,
    CLEANUP_KIND_ORPHAN_INPUT,
    CLEANUP_KIND_STAGING,
    CLEANUP_KIND_TEMP,
    JobRecord,
    Janitor,
    OutputTooLargeError,
    QuotaManager,
    QueueFullError,
    QuotaPolicy,
    RetentionPolicy,
    StorageQuotaError,
    StoreProtocol,
    safe_delete_tree,
)


class FakeStore(StoreProtocol):
    """Small in-memory store used for lifecycle unit tests."""

    def __init__(self, records: list[JobRecord] | list[dict[str, object]]):
        self.records = list(records)
        self.claimed: set[tuple[str, str]] = set()
        self.claim_calls: list[tuple[str, str]] = []
        self.complete_calls: list[tuple[str, str, int, str | None]] = []

    def list_records(self) -> list[JobRecord | dict[str, object]]:
        return list(self.records)

    def pending_and_bytes_stats(self) -> dict[str, int]:
        pending = 0
        reserved = 0
        for record in self.records:
            state = record["state"] if isinstance(record, dict) else record.state
            if state in {"queued", "running"}:
                pending += 1
            input_bytes = record["input_bytes"] if isinstance(record, dict) else record.input_bytes
            output_bytes = record["output_bytes"] if isinstance(record, dict) else record.output_bytes
            reserved_output = (
                record.get("reserved_output_bytes")
                if isinstance(record, dict)
                else record.reserved_output_bytes
            )
            reserved += int(input_bytes) + int(reserved_output or output_bytes)
        return {"pending_count": pending, "reserved_bytes": reserved}

    def claim_cleanup(self, job_id: str, kind: str, now: float) -> str | None:
        _ = now
        key = (job_id, kind)
        if key in self.claimed:
            return None
        self.claimed.add(key)
        self.claim_calls.append(key)
        return f"{job_id}:{kind}"

    def complete_cleanup(
        self,
        job_id: str,
        kind: str,
        *,
        lease_id: str,
        deleted_bytes: int,
        error: str | None = None,
    ) -> None:
        _ = lease_id
        self.complete_calls.append((job_id, kind, deleted_bytes, error))
        self.claimed.discard((job_id, kind))


class FakeClock:
    def __init__(self, start: float):
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _old(path: Path, age_seconds: float, now: float) -> None:
    mtime = now - age_seconds
    try:
        os.utime(path, (mtime, mtime), follow_symlinks=False)
    except TypeError:
        os.utime(path, (mtime, mtime))


class QuotaManagerTests(unittest.TestCase):
    def test_queue_capacity_storage_limits(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            now = 1_000_000.0
            records = [
                JobRecord(
                    job_id="pending",
                    state="running",
                    input_path=root_path / "pending.in",
                    output_path=root_path / "pending.out",
                    created_at=now,
                    input_bytes=120,
                    output_bytes=30,
                )
            ]

            store = FakeStore(records)
            manager = QuotaManager(
                QuotaPolicy(
                    max_pending=1,
                    max_data_bytes=200,
                    min_free_bytes=256,
                    max_output_bytes=60,
                ),
                disk_usage=lambda _path: type("_Usage", (), {"free": 2048}),
            )
            with self.assertRaises(QueueFullError):
                manager.check(
                    store,
                    input_bytes=10,
                    data_root=root_path,
                )

            records[0].state = "succeeded"
            manager = QuotaManager(
                QuotaPolicy(
                    max_pending=10,
                    max_data_bytes=200,
                    min_free_bytes=256,
                    max_output_bytes=60,
                ),
                disk_usage=lambda _path: type("_Usage", (), {"free": 2048}),
            )
            with self.assertRaises(StorageQuotaError):
                manager.check(store, input_bytes=100, data_root=root_path)

            with self.assertRaises(OutputTooLargeError):
                manager.check(
                    store,
                    input_bytes=10,
                    expected_output_bytes=80,
                    data_root=root_path,
                )

            manager = QuotaManager(
                QuotaPolicy(
                    max_pending=10,
                    max_data_bytes=200,
                    min_free_bytes=2048,
                    max_output_bytes=60,
                ),
                disk_usage=lambda _path: type("_Usage", (), {"free": 1024}),
            )
            with self.assertRaises(StorageQuotaError):
                manager.check(store, input_bytes=10, data_root=root_path)

            manager = QuotaManager(
                QuotaPolicy(
                    max_pending=10,
                    max_data_bytes=2000,
                    min_free_bytes=256,
                    max_output_bytes=60,
                ),
                disk_usage=lambda _path: type("_Usage", (), {"free": 2048}),
            )
            manager.check(store, input_bytes=10, data_root=root_path)

    def test_expected_output_defaults_to_policy_max_output(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            records = [
                JobRecord(
                    job_id="running",
                    state="running",
                    input_path=root_path / "a",
                    output_path=root_path / "b",
                    created_at=0.0,
                    input_bytes=1,
                    output_bytes=1,
                )
            ]
            store = FakeStore(records)
            manager = QuotaManager(
                QuotaPolicy(
                    max_pending=10,
                    max_data_bytes=100,
                    min_free_bytes=10,
                    max_output_bytes=80,
                ),
                disk_usage=lambda _path: type("_Usage", (), {"free": 89}),
            )
            with self.assertRaises(StorageQuotaError):
                manager.check(store, input_bytes=1, data_root=root_path)


class JanitorTests(unittest.TestCase):
    def test_success_and_failed_ttls_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)

            input_root = root / "inputs"
            output_root = root / "outputs"
            tomb_root = root / "tombstones"
            for directory in (input_root, output_root, tomb_root):
                directory.mkdir()

            succeeded = JobRecord(
                job_id="ok",
                state="succeeded",
                created_at=now(),
                finished_at=now() - 120,
                input_path=input_root / "ok",
                output_path=output_root / "ok",
                input_expires_at=now() - 10,
                tombstone_expires_at=now() + 999_999,
            )
            failed = JobRecord(
                job_id="bad",
                state="failed",
                created_at=now(),
                finished_at=now() - 120,
                input_path=input_root / "bad",
                output_path=output_root / "bad",
                input_expires_at=now() - 10,
                tombstone_expires_at=now() + 999_999,
            )
            _touch(succeeded.input_path / "input.pdf")
            _touch(succeeded.output_path / "out.txt")
            _touch(failed.input_path / "input.pdf")
            _touch(failed.output_path / "out.txt")

            janitor = Janitor(
                FakeStore([succeeded, failed]),
                retention=RetentionPolicy(
                    success_output_ttl=999_999,
                    failed_output_ttl=10,
                    input_ttl=20,
                    tombstone_ttl=999_999,
                ),
                input_root=input_root,
                output_root=output_root,
                tombstone_root=tomb_root,
                now=now,
            )

            janitor.run_once()

            self.assertFalse((output_root / "bad").exists())
            self.assertTrue((output_root / "ok").exists())

    def test_input_cleanup_happens_before_output_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)
            event: list[str] = []

            def tracked_delete(_root: Path, target: Path | str) -> int:
                target_text = str(target)
                if "inputs" in target_text:
                    event.append("input")
                elif "outputs" in target_text:
                    event.append("output")
                return safe_delete_tree(_root, target)

            record = JobRecord(
                job_id="job-1",
                state="succeeded",
                created_at=now() - 1000,
                finished_at=now() - 1000,
                input_path=root / "inputs" / "job-1",
                output_path=root / "outputs" / "job-1",
                input_expires_at=now() - 10,
                output_expires_at=now() - 10,
            )
            _touch(record.input_path / "in.txt")
            _touch(record.output_path / "out.txt")

            janitor = Janitor(
                FakeStore([record]),
                retention=RetentionPolicy(
                    input_ttl=86400,
                    success_output_ttl=9999,
                    failed_output_ttl=9999,
                    tombstone_ttl=9999,
                ),
                input_root=root / "inputs",
                output_root=root / "outputs",
                tombstone_root=root / "tombstones",
                now=now,
                cleanup_delete_fn=tracked_delete,
            )
            janitor.run_once()

            self.assertEqual(event, ["input", "output"])
            self.assertFalse((root / "inputs" / "job-1").exists())
            self.assertFalse((root / "outputs" / "job-1").exists())

    def test_active_staging_dir_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)

            staging_root = root / "staging"
            staging_root.mkdir()
            active_dir = staging_root / "active"
            stale_dir = staging_root / "stale"
            active_dir.mkdir()
            stale_dir.mkdir()
            _old(active_dir, 10_000, now())
            _old(stale_dir, 10_000, now())

            janitor = Janitor(
                FakeStore([
                    JobRecord(
                        job_id="active",
                        state="running",
                        input_path=root / "inputs" / "active",
                        output_path=root / "outputs" / "active",
                        created_at=now(),
                    )
                ]),
                retention=RetentionPolicy(staging_ttl=100),
                input_root=root / "inputs",
                output_root=root / "outputs",
                tombstone_root=root / "tombstones",
                staging_root=staging_root,
                now=now,
            )

            janitor.run_once()

            self.assertTrue(active_dir.exists())
            self.assertFalse(stale_dir.exists())

    def test_orphan_upload_in_temp_root_expires(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)
            temp_root = root / "temp"
            temp_root.mkdir()
            orphan = temp_root / "docling-upload-orphan.pdf"
            protected = temp_root / "docling-upload-active.pdf"
            _touch(orphan)
            _touch(protected)
            _old(orphan, 4000, now())
            _old(protected, 4000, now())
            active_names = {protected.name}
            janitor = Janitor(
                FakeStore([]),
                retention=RetentionPolicy(temp_ttl=3600),
                input_root=root / "inputs",
                output_root=root / "outputs",
                tombstone_root=root / "tombstones",
                temp_root=temp_root,
                now=now,
                protected_temp_entries=lambda: set(active_names),
            )
            janitor.run_once()
            self.assertFalse(orphan.exists())
            self.assertTrue(protected.exists())
            active_names.clear()
            janitor.run_once()
            self.assertFalse(protected.exists())

    def test_orphan_input_directories_are_reaped_with_temp_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)
            input_root = root / "inputs"
            input_root.mkdir()

            stale_job = str(uuid.uuid4())
            stale_dir = input_root / stale_job
            stale_dir.mkdir()
            _touch(stale_dir / "source.pdf")
            _old(stale_dir, 4_000, now())

            fresh_job = str(uuid.uuid4())
            fresh_dir = input_root / fresh_job
            fresh_dir.mkdir()
            _touch(fresh_dir / "source.pdf")
            _old(fresh_dir, 30, now())

            pending_job = str(uuid.uuid4())
            pending_dir = input_root / pending_job
            pending_dir.mkdir()
            _touch(pending_dir / "source.pdf")
            _old(pending_dir, 4_000, now())

            known_job = str(uuid.uuid4())
            known_dir = input_root / known_job
            known_dir.mkdir()
            _touch(known_dir / "source.pdf")
            _old(known_dir, 4_000, now())

            janitor = Janitor(
                FakeStore(
                    [
                        JobRecord(
                            job_id=known_job,
                            state="succeeded",
                            created_at=now(),
                            finished_at=now() - 10,
                            input_path=known_dir,
                            output_path=Path("outputs") / known_job,
                        )
                    ]
                ),
                retention=RetentionPolicy(temp_ttl=60),
                input_root=input_root,
                output_root=root / "outputs",
                tombstone_root=root / "tombs",
                now=now,
                pending_inputs=lambda: {pending_job},
            )

            janitor.run_once()

            self.assertFalse(stale_dir.exists())
            self.assertTrue(fresh_dir.exists())
            self.assertTrue(pending_dir.exists())
            self.assertTrue(known_dir.exists())

    def test_orphan_input_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)
            input_root = root / "inputs"
            input_root.mkdir()

            orphan = input_root / str(uuid.uuid4())
            if os.name != "nt":
                orphan.symlink_to(root / "outside-target")
            else:
                # Best-effort on platforms where symlink may need privileges;
                # this still verifies the skip path in a deterministic way.
                (orphan).write_text("", encoding="utf-8")

            _old(orphan, 4_000, now())

            janitor = Janitor(
                FakeStore([]),
                retention=RetentionPolicy(temp_ttl=60),
                input_root=input_root,
                output_root=root / "outputs",
                tombstone_root=root / "tombs",
                now=now,
            )
            janitor.run_once()
            if os.name != "nt":
                self.assertTrue(orphan.is_symlink())
            else:
                self.assertTrue(orphan.exists())

    def test_orphan_input_cleanup_retries_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)
            input_root = root / "inputs"
            input_root.mkdir()

            orphan = input_root / str(uuid.uuid4())
            orphan.mkdir()
            _touch(orphan / "source.pdf")
            _old(orphan, 4_000, now())

            attempts: dict[str, int] = {}

            def flaky_cleanup(_root: Path, target: Path | str) -> int:
                attempts[str(target)] = attempts.get(str(target), 0) + 1
                if attempts[str(target)] == 1:
                    raise PermissionError("temp failure")
                return safe_delete_tree(_root, target)

            fake_store = FakeStore([])
            janitor = Janitor(
                fake_store,
                retention=RetentionPolicy(temp_ttl=60),
                input_root=input_root,
                output_root=root / "outputs",
                tombstone_root=root / "tombs",
                now=now,
                cleanup_delete_fn=flaky_cleanup,
            )

            janitor.run_once()
            self.assertTrue(orphan.exists())
            self.assertEqual(fake_store.complete_calls[0][1], CLEANUP_KIND_ORPHAN_INPUT)
            self.assertIsNotNone(fake_store.complete_calls[0][3])

            janitor.run_once()
            self.assertFalse(orphan.exists())
            self.assertIsNone(fake_store.complete_calls[1][3])

    def test_cleanup_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)
            attempts: dict[str, int] = {}

            def flaky_delete(_root: Path, target: Path | str) -> int:
                key = str(target)
                attempts[key] = attempts.get(key, 0) + 1
                if attempts[key] == 1:
                    raise PermissionError("temporary failure")
                return safe_delete_tree(_root, target)

            record = JobRecord(
                job_id="retry",
                state="failed",
                created_at=now() - 100,
                finished_at=now() - 100,
                input_path=root / "inputs" / "retry",
                output_path=root / "outputs" / "retry",
                input_expires_at=now() - 10,
                output_expires_at=now() + 999_999,
            )
            _touch(record.input_path / "in.txt")

            store = FakeStore([record])
            janitor = Janitor(
                store,
                retention=RetentionPolicy(failed_output_ttl=999_999, tombstone_ttl=999_999),
                input_root=root / "inputs",
                output_root=root / "outputs",
                tombstone_root=root / "tombstones",
                now=now,
                cleanup_delete_fn=flaky_delete,
            )

            janitor.run_once()
            self.assertTrue((root / "inputs" / "retry").exists())
            self.assertEqual(len(store.complete_calls), 1)
            self.assertEqual(store.complete_calls[0][:2], ("retry", CLEANUP_KIND_INPUT))
            self.assertIsNotNone(store.complete_calls[0][3])

            janitor.run_once()
            self.assertFalse((root / "inputs" / "retry").exists())
            success_calls = [
                item
                for item in store.complete_calls
                if item[0] == "retry" and item[1] == CLEANUP_KIND_INPUT and item[3] is None
            ]
            self.assertEqual(len(success_calls), 1)

    def test_metadata_tombstone_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            now = FakeClock(1_000_000.0)

            tomb_root = root / "tombstones"
            tomb_root.mkdir()
            record = JobRecord(
                job_id="meta",
                state="failed",
                created_at=now() - 10,
                finished_at=now() - 10,
                input_path=root / "inputs" / "meta",
                output_path=root / "outputs" / "meta",
                tombstone_path=tomb_root / "meta",
                input_expires_at=now() + 1,
                output_expires_at=now() + 1,
                tombstone_expires_at=now() + 1_000_000,
            )
            tomb_path = record.tombstone_path
            assert tomb_path is not None
            _touch(tomb_path / "metadata.json")

            janitor = Janitor(
                FakeStore([record]),
                retention=RetentionPolicy(tombstone_ttl=100),
                input_root=root / "inputs",
                output_root=root / "outputs",
                tombstone_root=tomb_root,
                now=now,
            )

            janitor.run_once()
            self.assertTrue(tomb_path.exists())

            record.tombstone_expires_at = now() - 1
            janitor.run_once()
            self.assertFalse(tomb_path.exists())

    def test_janitor_start_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            janitor = Janitor(
                FakeStore([]),
                retention=RetentionPolicy(),
                input_root=root,
                output_root=root,
                tombstone_root=root,
                staging_root=root / "staging",
                temp_root=root / "temp",
            )
            janitor.start()
            sleep(0.02)
            janitor.stop(wait=2.0)
            janitor.stop(wait=2.0)
            janitor.run_once()

    def test_maintenance_purges_run_and_failures_are_retried(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            calls: list[str] = []

            def failing() -> None:
                calls.append("failing")
                raise RuntimeError("transient")

            def succeeding() -> None:
                calls.append("succeeding")

            janitor = Janitor(
                FakeStore([]),
                retention=RetentionPolicy(),
                input_root=root,
                output_root=root,
                tombstone_root=root,
                maintenance=(failing, succeeding),
            )
            janitor.run_once()
            janitor.run_once()
            self.assertEqual(
                ["failing", "succeeding", "failing", "succeeding"], calls
            )


if __name__ == "__main__":
    unittest.main()
