from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import sqlite3
import uuid

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_service.persistence import (  # noqa: E402
    RuntimeConfigConflict,
    SQLiteStore,
)


def _tmp_db_root() -> str:
    return tempfile.mkdtemp(prefix="docling-store-")


def _make_job_ids() -> tuple[str, str, str]:
    return (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))


class SQLiteStoreTests(unittest.TestCase):
    def _new_store(self, root: str) -> SQLiteStore:
        input_root = Path(root) / "input"
        output_root = Path(root) / "output"
        input_root.mkdir(exist_ok=True)
        output_root.mkdir(exist_ok=True)
        return SQLiteStore(
            Path(root) / "docling.sqlite",
            input_root=input_root,
            output_root=output_root,
        )

    def _cleanup_claim_count(self, store: SQLiteStore, job_id: str) -> int:
        return len(
            store._conn.execute(
                "SELECT 1 FROM cleanup_claims WHERE job_id = ?",
                (job_id,),
            ).fetchall()
        )

    def test_migrations_are_idempotent_and_concurrent(self) -> None:
        root = _tmp_db_root()
        store = self._new_store(root)
        store.close()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(20):
                    local = self._new_store(root)
                    local.close()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        conn = sqlite3.connect(Path(root) / "docling.sqlite")
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(2, row[0])
            count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(2, count)
        finally:
            conn.close()

    def test_job_crud_and_cursor_list(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_a, job_b, job_c = _make_job_ids()
            now = datetime.now(timezone.utc)
            self.assertEqual(
                "queued",
                store.create_job(
                    job_id=job_a,
                    original_name="a.pdf",
                    input_path=f"{root}/input/a.pdf",
                    output_dir=f"{root}/output/a",
                    created_at=(now - timedelta(minutes=3)).isoformat(),
                )["state"],
            )
            self.assertEqual(
                "queued",
                store.create_job(
                    job_id=job_b,
                    original_name="b.pdf",
                    input_path=f"{root}/input/b.pdf",
                    output_dir=f"{root}/output/b",
                    created_at=(now - timedelta(minutes=2)).isoformat(),
                )["state"],
            )
            self.assertEqual(
                "queued",
                store.create_job(
                    job_id=job_c,
                    original_name="c.pdf",
                    input_path=f"{root}/input/c.pdf",
                    output_dir=f"{root}/output/c",
                    created_at=(now - timedelta(minutes=1)).isoformat(),
                )["state"],
            )

            page_one = store.list_jobs(limit=2)
            self.assertEqual(2, len(page_one["items"]))
            self.assertIn("next_cursor", page_one)
            self.assertIsNotNone(page_one["next_cursor"])

            page_two = store.list_jobs(limit=2, cursor=page_one["next_cursor"])
            self.assertEqual(1, len(page_two["items"]))
            self.assertEqual(job_a, page_two["items"][0]["job_id"])
            self.assertEqual(
                "invalid_cursor",
                store.list_jobs(
                    limit=2,
                    state="queued",
                    cursor=page_one["next_cursor"],
                )["error"],
            )
            self.assertEqual(
                {"job_id": job_b},
                {"job_id": store.get_job(job_b)["job_id"]},
            )
            updated = store.update_job(job_b, state="succeeded", exit_code=0)
            self.assertEqual("succeeded", updated["state"])
            self.assertEqual("running", store.update_job(job_a, state="running")["state"])
            self.assertEqual(
                "job_state_conflict",
                store.update_job(job_a, state="queued")["error"],
            )

    def test_v2_progress_fifo_position_and_runtime_cas(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            first, second, third = _make_job_ids()
            common_created = "2024-01-01T00:00:00+00:00"
            for job_id in (first, second, third):
                store.create_job(
                    job_id=job_id,
                    original_name=f"{job_id}.pdf",
                    input_path=f"{root}/input/{job_id}.pdf",
                    output_dir=f"{root}/output/{job_id}",
                    created_at=common_created,
                )
            self.assertEqual(1, store.get_job(first)["queue_position"])
            self.assertEqual(2, store.get_job(second)["queue_position"])
            self.assertEqual(3, store.get_job(third)["queue_position"])

            progress = store.update_progress(first, "extracting", message="working")
            self.assertEqual("extracting", progress["progress_stage"])
            self.assertIsNone(progress["progress_percent"])
            self.assertEqual("working", progress["progress_message"])
            running = store.update_job(first, state="running")
            self.assertIsNone(running["queue_position"])
            self.assertEqual("running", running["progress_stage"])

            initial = store.runtime_config_snapshot()
            self.assertEqual(0, initial["revision"])
            updated = store.update_runtime_config(
                initial["revision"], {"input_ttl_seconds": 120}
            )
            self.assertEqual(1, updated["revision"])
            self.assertEqual(120, updated["overrides"]["input_ttl_seconds"])
            with self.assertRaises(RuntimeConfigConflict):
                store.update_runtime_config(0, {})

    def test_concurrent_finalizers_cannot_overwrite_terminal_state(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="race.pdf",
                input_path=f"{root}/input/race.pdf",
                output_dir=f"{root}/output/race",
                state="running",
            )
            barrier = threading.Barrier(2)
            results: list[dict] = []

            def finalize(state: str) -> None:
                barrier.wait()
                results.append(store.finalize_job(job_id, state=state, manifest=[]))

            workers = [
                threading.Thread(target=finalize, args=("succeeded",)),
                threading.Thread(target=finalize, args=("failed",)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertIn(store.get_job(job_id)["state"], {"succeeded", "failed"})
            self.assertEqual(
                1,
                sum(result.get("error") == "job_terminal_conflict" for result in results),
            )

    def test_manifest_replace_and_list(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="m.pdf",
                input_path=f"{root}/input/m.pdf",
                output_dir=f"{root}/output/m",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            first = store.replace_manifest(
                job_id=job_id,
                manifest=[
                    {"path": "document.md", "size_bytes": 11, "sha256": "a" * 64, "media_type": "text/markdown"},
                    {"path": "document.html", "size_bytes": 22, "sha256": "b" * 64, "media_type": "text/html"},
                ],
            )
            self.assertEqual(2, first["count"])
            second = store.replace_manifest(
                job_id=job_id,
                manifest=[{"path": "document.pdf", "size_bytes": 9, "sha256": "c" * 64, "media_type": "application/pdf"}],
            )
            self.assertEqual(1, second["count"])
            manifest = store.list_manifest(job_id)
            self.assertEqual(1, manifest["count"])
            self.assertEqual("document.pdf", manifest["items"][0]["path"])

    def test_manifest_rejects_unsafe_or_ambiguous_entries(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="safe.pdf",
                input_path=f"{root}/input/safe.pdf",
                output_dir=f"{root}/output/safe",
            )
            for manifest in (
                [{"path": "../escape", "size_bytes": 1, "sha256": "a" * 64}],
                [{"path": "/absolute", "size_bytes": 1, "sha256": "a" * 64}],
                [{"path": "file", "size_bytes": -1, "sha256": "a" * 64}],
                [{"path": "file", "size_bytes": 1, "sha256": "short"}],
                [
                    {"path": "same", "size_bytes": 1, "sha256": "a" * 64},
                    {"path": "same", "size_bytes": 1, "sha256": "a" * 64},
                ],
            ):
                with self.subTest(manifest=manifest):
                    self.assertIn(
                        store.replace_manifest(job_id, manifest)["error"],
                        {"invalid_path", "invalid_size_bytes", "invalid_sha256"},
                    )
            valid = store.replace_manifest(
                job_id,
                [{"path": "nested/document.md", "size_bytes": 3, "sha256": "b" * 64}],
            )
            self.assertEqual(1, valid["count"])
            self.assertEqual(64, len(store.get_job(job_id)["manifest_sha256"]))

    def test_pending_and_bytes_stats(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_a, job_b, job_c = _make_job_ids()
            store.create_job(
                job_id=job_a,
                original_name="a.pdf",
                input_path=f"{root}/input/a.pdf",
                output_dir=f"{root}/output/a",
                state="queued",
            )
            store.create_job(
                job_id=job_b,
                original_name="b.pdf",
                input_path=f"{root}/input/b.pdf",
                output_dir=f"{root}/output/b",
                state="running",
            )
            store.create_job(
                job_id=job_c,
                original_name="c.pdf",
                input_path=f"{root}/input/c.pdf",
                output_dir=f"{root}/output/c",
                state="succeeded",
            )
            store.replace_manifest(
                job_a,
                [{"path": "a", "size_bytes": 10}],
            )
            store.replace_manifest(job_b, [{"path": "b", "size_bytes": 20}])
            store.replace_manifest(job_c, [{"path": "c", "size_bytes": 30}])
            stats = store.pending_and_bytes_stats()
            self.assertEqual(2, stats["pending_jobs"])
            self.assertEqual(30, stats["pending_bytes"])
            self.assertEqual(60, stats["stored_bytes"])

    def test_lease_flow(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="d.pdf",
                input_path=f"{root}/input/d.pdf",
                output_dir=f"{root}/output/d",
                state="succeeded",
            )
            store.replace_manifest(
                job_id,
                [
                    {"path": "document.pdf", "size_bytes": 0},
                    {"path": "expired.txt", "size_bytes": 0},
                ],
            )
            lease_a = store.acquire_download_lease(
                job_id,
                "document.pdf",
                holder="worker-a",
                ttl_seconds=10,
            )
            self.assertEqual(job_id, lease_a["job_id"])
            lease_a2 = store.acquire_download_lease(
                job_id,
                "document.pdf",
                holder="worker-a",
                ttl_seconds=10,
            )
            self.assertEqual(lease_a["lease_id"], lease_a2["lease_id"])
            self.assertEqual(
                {"error": "lease_conflict"},
                store.acquire_download_lease(job_id, "document.pdf", holder="worker-b"),
            )
            renewed = store.renew_download_lease(lease_a["lease_id"], ttl_seconds=120)
            self.assertIn("expires_at", renewed)
            self.assertNotEqual(lease_a["expires_at"], renewed["expires_at"])
            released = store.release_download_lease(lease_a["lease_id"])
            self.assertIsNotNone(released["released_at"])

            # expired candidate path
            expired = store.acquire_download_lease(
                job_id,
                "expired.txt",
                holder="w",
                ttl_seconds=1,
            )
            time.sleep(1.05)
            expired_candidates = store.list_expired_download_leases()
            self.assertEqual(1, expired_candidates["count"])
            self.assertEqual(expired["lease_id"], expired_candidates["items"][0]["lease_id"])

    def test_list_expired_tombstone_and_tombstone(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            old_job = _make_job_ids()[0]
            done = datetime.now(timezone.utc) - timedelta(minutes=30)
            store.create_job(
                job_id=old_job,
                original_name="e.pdf",
                input_path=f"{root}/input/e.pdf",
                output_dir=f"{root}/output/e",
                state="succeeded",
            )
            store.update_job(
                old_job,
                finished_at=done.isoformat(),
                state="succeeded",
                exit_code=0,
            )
            candidates = store.list_expired_job_candidates(max_age_seconds=60 * 10,)
            self.assertEqual(1, candidates["count"])
            tombstoned = store.tombstone_jobs([old_job], reason="retention")
            self.assertEqual(1, tombstoned["updated"])
            row = store.get_job(old_job)
            self.assertEqual("retention", row["tombstone_reason"])

    def test_import_legacy_state_jobs_is_idempotent(self) -> None:
        root = Path(_tmp_db_root())
        input_root = root / "input"
        output_root = root / "output"
        input_root.mkdir()
        output_root.mkdir()
        state_root = root / "state"
        jobs_dir = state_root / "jobs"
        jobs_dir.mkdir(parents=True)
        valid_job = str(uuid.uuid4())
        terminal_job = str(uuid.uuid4())
        invalid_uuid_job = "invalid-uuid"
        bad_root_job = str(uuid.uuid4())
        with open(jobs_dir / "valid.json", "w", encoding="utf-8") as fp:
            fp.write(
                f"""{{"job_id":"{valid_job}","state":"queued","original_name":"v.pdf","input_path":"{input_root / "v.pdf"}","output_dir":"{output_root / "v"}","created_at":"2020-01-01T00:00:00+00:00","started_at":null,"finished_at":null,"exit_code":null,"error":null}}"""
            )
        with open(jobs_dir / "terminal.json", "w", encoding="utf-8") as fp:
            fp.write(
                f"""{{"job_id":"{terminal_job}","state":"succeeded","original_name":"done.pdf","input_path":"{input_root / "done.pdf"}","output_dir":"{output_root / "done"}","created_at":"2020-01-01T00:00:00+00:00","started_at":"2020-01-01T00:01:00+00:00","finished_at":"2020-01-01T00:10:00+00:00","exit_code":0,"error":null}}"""
            )
        with open(jobs_dir / "invalid_uuid.json", "w", encoding="utf-8") as fp:
            fp.write(
                f"""{{"job_id":"{invalid_uuid_job}","state":"queued","original_name":"x.pdf","input_path":"{input_root / "x.pdf"}","output_dir":"{output_root / "x"}","created_at":"2020-01-01T00:00:00+00:00","started_at":null,"finished_at":null,"exit_code":null,"error":null}}"""
            )
        with open(jobs_dir / "bad_root.json", "w", encoding="utf-8") as fp:
            fp.write(
                f"""{{"job_id":"{bad_root_job}","state":"queued","original_name":"y.pdf","input_path":"/tmp/outside-path/y.pdf","output_dir":"/tmp/outside-path/y","created_at":"2020-01-01T00:00:00+00:00","started_at":null,"finished_at":null,"exit_code":null,"error":null}}"""
            )

        with SQLiteStore(
            root / "docling.sqlite", input_root=input_root, output_root=output_root
        ) as store:
            first = store.import_legacy_state_jobs(
                state_root,
                runtime_config_revision=3,
                input_ttl_seconds=120,
                success_output_ttl_seconds=240,
                failed_output_ttl_seconds=180,
                job_ttl_seconds=360,
            )
            self.assertEqual(2, len(first["imported"]))
            self.assertEqual(2, len(first["skipped"]))
            second = store.import_legacy_state_jobs(state_root)
            self.assertEqual(0, len(second["imported"]))
            self.assertEqual({}, store.get_job(invalid_uuid_job))
            imported = store.get_job(valid_job)
            self.assertEqual("queued", imported["progress_stage"])
            self.assertEqual(1, imported["queue_position"])
            self.assertEqual(3, imported["runtime_config_revision"])
            self.assertEqual(240, imported["success_output_ttl_seconds"])
            self.assertEqual("2020-01-01T00:02:00+00:00", imported["input_expires_at"])
            terminal = store.get_job(terminal_job)
            self.assertEqual("succeeded", terminal["progress_stage"])
            self.assertEqual(100, terminal["progress_percent"])
            self.assertEqual(
                "2020-01-01T00:14:00+00:00",
                terminal["output_expires_at"],
            )
            self.assertEqual(
                "2020-01-01T00:16:00+00:00",
                terminal["tombstone_expires_at"],
            )

    def test_expired_output_cannot_acquire_download_lease(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="expired.pdf",
                input_path=f"{root}/input/expired.pdf",
                output_dir=f"{root}/output/{job_id}",
                state="succeeded",
                output_expires_at="2020-01-01T00:00:00+00:00",
            )
            result = store.acquire_download_lease(
                job_id,
                "__archive__",
                ttl_seconds=60,
            )
            self.assertEqual("output_expired", result["error"])

    def test_idempotency_keys(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="f.pdf",
                input_path=f"{root}/input/f.pdf",
                output_dir=f"{root}/output/f",
                state="queued",
            )
            first = store.register_idempotency_key("k1", job_id)
            second = store.register_idempotency_key("k1", job_id)
            self.assertEqual(first["idempotency_key"], second["idempotency_key"])
            self.assertEqual(job_id, first["job_id"])
            self.assertEqual(job_id, store.resolve_idempotency_key("k1")["job_id"])

    def test_atomic_idempotent_create_replays_or_conflicts(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            first_job, second_job, third_job = _make_job_ids()
            fields = {
                "original_name": "same.pdf",
                "input_path": f"{root}/input/same.pdf",
                "output_dir": f"{root}/output/same",
                "input_size_bytes": 10,
                "reserved_output_bytes": 20,
            }
            created = store.create_job_with_idempotency(
                idempotency_key="workflow-1",
                job_id=first_job,
                request_fingerprint="fingerprint-a",
                **fields,
            )
            self.assertFalse(created["_idempotent_replay"])
            replayed = store.create_job_with_idempotency(
                idempotency_key="workflow-1",
                job_id=second_job,
                request_fingerprint="fingerprint-a",
                **fields,
            )
            self.assertEqual(first_job, replayed["job_id"])
            self.assertTrue(replayed["_idempotent_replay"])
            conflict = store.create_job_with_idempotency(
                idempotency_key="workflow-1",
                job_id=third_job,
                request_fingerprint="fingerprint-b",
                **fields,
            )
            self.assertEqual("idempotency_conflict", conflict["error"])

    def test_cleanup_failure_retries_and_tombstone_hard_deletes(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="cleanup.pdf",
                input_path=f"{root}/input/cleanup.pdf",
                output_dir=f"{root}/output/cleanup",
                state="succeeded",
                input_size_bytes=10,
                output_size_bytes=20,
            )
            now = datetime.now(timezone.utc).timestamp()
            first_lease = store.claim_cleanup(job_id, "input", now)
            self.assertTrue(first_lease)
            store.complete_cleanup(
                job_id,
                "input",
                lease_id=str(first_lease),
                deleted_bytes=0,
                error="busy",
            )
            second_lease = store.claim_cleanup(job_id, "input", now + 1)
            self.assertTrue(second_lease)
            store.complete_cleanup(
                job_id,
                "input",
                lease_id=str(second_lease),
                deleted_bytes=10,
            )
            self.assertEqual(0, store.get_job(job_id)["input_size_bytes"])

            tombstone_lease = store.claim_cleanup(job_id, "tombstone", now)
            self.assertTrue(tombstone_lease)
            store.complete_cleanup(
                job_id,
                "tombstone",
                lease_id=str(tombstone_lease),
                deleted_bytes=0,
            )
            self.assertEqual({}, store.get_job(job_id))

    def test_non_job_cleanup_claim_retries_then_clears_on_success(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            marker = _make_job_ids()[0]
            now = datetime.now(timezone.utc).timestamp()

            staging_lease = store.claim_cleanup(marker, "staging_dir", now)
            self.assertTrue(staging_lease)
            store.complete_cleanup(
                marker,
                "staging_dir",
                lease_id=str(staging_lease),
                deleted_bytes=0,
                error="busy",
            )
            self.assertEqual(1, self._cleanup_claim_count(store, marker))
            staging_retry_lease = store.claim_cleanup(marker, "staging_dir", now + 1)
            self.assertTrue(staging_retry_lease)
            self.assertNotEqual(staging_lease, staging_retry_lease)
            store.complete_cleanup(
                marker,
                "staging_dir",
                lease_id=str(staging_retry_lease),
                deleted_bytes=11,
                error=None,
            )
            self.assertEqual(0, self._cleanup_claim_count(store, marker))

    def test_orphan_input_cleanup_claim_retries_then_clears_on_success(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            marker = _make_job_ids()[0]
            now = datetime.now(timezone.utc).timestamp()

            first_lease = store.claim_cleanup(marker, "orphan_input", now)
            self.assertTrue(first_lease)
            store.complete_cleanup(
                marker,
                "orphan_input",
                lease_id=str(first_lease),
                deleted_bytes=0,
                error="transient",
            )
            self.assertEqual(1, self._cleanup_claim_count(store, marker))

            second_lease = store.claim_cleanup(marker, "orphan_input", now + 1)
            self.assertTrue(second_lease)
            self.assertNotEqual(first_lease, second_lease)
            store.complete_cleanup(
                marker,
                "orphan_input",
                lease_id=str(second_lease),
                deleted_bytes=17,
                error=None,
            )
            self.assertEqual(0, self._cleanup_claim_count(store, marker))

            temp_lease = store.claim_cleanup(marker, "temp_dir", now + 401)
            self.assertTrue(temp_lease)
            store.complete_cleanup(
                marker,
                "temp_dir",
                lease_id=str(temp_lease),
                deleted_bytes=12,
                error=None,
            )
            self.assertEqual(0, self._cleanup_claim_count(store, marker))

    def test_tombstone_success_clears_all_job_cleanup_claims(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="cleanup_all.pdf",
                input_path=f"{root}/input/cleanup_all.pdf",
                output_dir=f"{root}/output/cleanup_all",
                state="succeeded",
                input_size_bytes=20,
                output_size_bytes=30,
            )

            now = datetime.now(timezone.utc).timestamp()
            kinds = [
                "input",
                "output",
                "tombstone_dir",
                "staging_dir",
                "temp_dir",
            ]
            leases: dict[str, str] = {}
            for index, kind in enumerate(kinds):
                lease = store.claim_cleanup(job_id, kind, now + index)
                self.assertTrue(lease)
                leases[kind] = str(lease)

            for kind in kinds:
                store.complete_cleanup(
                    job_id,
                    kind,
                    lease_id=leases[kind],
                    deleted_bytes=0,
                    error=None,
                )
            self.assertEqual(2, self._cleanup_claim_count(store, job_id))

            tombstone_lease = store.claim_cleanup(job_id, "tombstone", now + 100)
            self.assertTrue(tombstone_lease)
            store.complete_cleanup(
                job_id,
                "tombstone",
                lease_id=str(tombstone_lease),
                deleted_bytes=0,
            )

            self.assertEqual({}, store.get_job(job_id))
            self.assertEqual(0, self._cleanup_claim_count(store, job_id))

    def test_cleanup_lease_fences_stale_workers_and_downloads(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="fence.pdf",
                input_path=f"{root}/input/fence.pdf",
                output_dir=f"{root}/output/fence",
                state="succeeded",
                input_size_bytes=10,
                output_size_bytes=20,
            )
            store.replace_manifest(
                job_id, [{"path": "document.md", "size_bytes": 20}]
            )
            base = datetime.now(timezone.utc)
            first = store.claim_cleanup(job_id, "input", base.timestamp())
            second = store.claim_cleanup(
                job_id, "input", (base + timedelta(seconds=301)).timestamp()
            )
            self.assertTrue(first)
            self.assertTrue(second)
            stale = store.complete_cleanup(
                job_id,
                "input",
                lease_id=str(first),
                deleted_bytes=10,
            )
            self.assertEqual("cleanup_lease_conflict", stale["error"])

            download = store.acquire_download_lease(
                job_id, "document.md", holder="reader", ttl_seconds=120
            )
            self.assertIn("lease_id", download)
            self.assertIsNone(
                store.claim_cleanup(job_id, "output", base.timestamp())
            )
            store.release_download_lease(download["lease_id"])
            output_cleanup = store.claim_cleanup(
                job_id, "output", base.timestamp()
            )
            self.assertTrue(output_cleanup)
            blocked = store.acquire_download_lease(
                job_id, "document.md", holder="late-reader", ttl_seconds=120
            )
            self.assertEqual("cleanup_in_progress", blocked["error"])

    def test_stale_webhook_claim_is_recovered_and_fenced(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="event.pdf",
                input_path=f"{root}/input/event.pdf",
                output_dir=f"{root}/output/event",
            )
            store.create_webhook_subscription(
                callback_url="https://example.test/hook",
                event_types=["docling.job.succeeded"],
                secret="0123456789abcdef",
            )
            base = datetime.now(timezone.utc)
            store.enqueue_webhook_event(
                event_type="docling.job.succeeded",
                payload={"job_id": job_id, "state": "succeeded"},
                now=base,
            )
            first = store.claim_webhook_delivery(
                now=base, lease_seconds=10, worker_id="worker-a"
            )
            second = store.claim_webhook_delivery(
                now=base + timedelta(seconds=11),
                lease_seconds=10,
                worker_id="worker-b",
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(2, second["attempts"])
            stale = store.complete_webhook_delivery(
                first["id"], success=True, worker_id="worker-a"
            )
            self.assertEqual("delivery_lease_conflict", stale["error"])
            completed = store.complete_webhook_delivery(
                second["id"], success=True, worker_id="worker-b"
            )
            self.assertEqual("succeeded", completed["status"])

    def test_webhook_subscription_and_deliveries(self) -> None:
        root = _tmp_db_root()
        with self._new_store(root) as store:
            job_id = _make_job_ids()[0]
            store.create_job(
                job_id=job_id,
                original_name="g.pdf",
                input_path=f"{root}/input/g.pdf",
                output_dir=f"{root}/output/g",
                state="queued",
            )
            all_subs = store.list_webhook_subscriptions()
            self.assertEqual(0, all_subs["count"])
            one = store.create_webhook_subscription(
                callback_url="https://example.test/a",
                event_types=["job.completed"],
                filters={"state": "succeeded"},
                enabled=True,
            )
            two = store.create_webhook_subscription(
                callback_url="https://example.test/b",
                event_types=["job.completed"],
                filters={"state": "succeeded"},
                enabled=True,
            )
            self.assertNotEqual({}, one)
            self.assertNotEqual({}, two)
            listed = store.list_webhook_subscriptions()
            self.assertEqual(2, listed["count"])
            store.update_job(job_id, state="succeeded")
            enqueued = store.enqueue_webhook_event(
                event_type="job.completed",
                payload={"job_id": job_id, "state": "succeeded"},
            )
            created = enqueued["created"]
            self.assertEqual(2, len(created))
            delivery_page = store.list_webhook_deliveries(limit=1)
            self.assertIsNotNone(delivery_page["next_cursor"])
            self.assertEqual(
                "invalid_cursor",
                store.list_webhook_deliveries(
                    status="failed",
                    limit=1,
                    cursor=delivery_page["next_cursor"],
                )["error"],
            )
            claimed = store.claim_webhook_delivery(worker_id="worker-1")
            self.assertIn(claimed.get("status"), {"in_progress"})
            completed = store.complete_webhook_delivery(
                int(claimed["id"]),
                success=True,
                status_code=200,
                worker_id="worker-1",
            )
            self.assertEqual("succeeded", completed["status"])
            pending = store.list_webhook_deliveries(status="pending")
            self.assertEqual(1, pending["count"])
            retry_item = store.retry_webhook_delivery(int(pending["items"][0]["id"]), error="downstream issue")
            self.assertEqual("retrying", retry_item["status"])

    def test_webhook_subscription_limit_is_atomic(self) -> None:
        root = _tmp_db_root()
        db_path = Path(root) / "docling.sqlite"
        input_root = Path(root) / "input"
        output_root = Path(root) / "output"
        input_root.mkdir()
        output_root.mkdir()
        stores = [
            SQLiteStore(
                db_path,
                input_root=input_root,
                output_root=output_root,
                max_webhook_subscriptions=1,
            )
            for _index in range(2)
        ]
        results: list[dict] = []
        errors: list[BaseException] = []
        ready = threading.Barrier(2)
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                ready.wait(timeout=5)
                results.append(
                    stores[index - 1].create_webhook_subscription(
                        callback_url=f"https://example.test/{index}",
                        event_types=["job.completed"],
                    )
                )
            except BaseException as exc:  # pragma: no cover - unexpected concurrency path
                with lock:
                    errors.append(exc)

        workers = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(timeout=10)
            self.assertFalse(worker_thread.is_alive())
        for store in stores:
            store.close()

        self.assertEqual([], errors)
        created = [result for result in results if result.get("id")]
        rejected = [result for result in results if result.get("error") == "subscription_limit"]
        self.assertEqual(1, len(created))
        self.assertEqual(1, len(rejected))

        with SQLiteStore(
            db_path,
            input_root=input_root,
            output_root=output_root,
            max_webhook_subscriptions=1,
        ) as store:
            self.assertEqual(1, store.list_webhook_subscriptions(include_disabled=True)["count"])


if __name__ == "__main__":
    unittest.main()
