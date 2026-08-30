from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from docling_service.archive import (
    _ArchiveIterator,
    _build_zip_info,
    ArchiveChangedError,
    ArchiveError,
    iter_archive,
)


def _build_entry(root: Path, path: Path, media_type: str = "text/plain") -> dict[str, object]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "path": str(path.relative_to(root).as_posix()),
        "size_bytes": len(payload),
        "sha256": digest,
        "media_type": media_type,
    }


class ArchiveTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_symlink_job_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real_root = base / "real"
            real_root.mkdir()
            output = real_root / "result.txt"
            output.write_text("result", encoding="utf-8")
            linked_root = base / "linked"
            linked_root.symlink_to(real_root, target_is_directory=True)
            manifest = [_build_entry(real_root, output)]

            with self.assertRaises(ArchiveError):
                b"".join(iter_archive(linked_root, manifest))

    def test_manifest_and_no_source_pdf_in_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("alpha", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "b.txt").write_text("bravo", encoding="utf-8")
            (root / "source.pdf").write_bytes(b"%PDF-1.7")
            manifest = [
                _build_entry(root, root / "source.pdf", "application/pdf"),
                _build_entry(root, root / "nested" / "b.txt"),
                _build_entry(root, root / "a.txt"),
            ]

            archive_bytes = b"".join(iter_archive(root, manifest))
            with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as handle:
                names = handle.namelist()
                self.assertNotIn("source.pdf", names)
                self.assertEqual(
                    ["a.txt", "nested/b.txt", "manifest.json"],
                    names,
                )
                manifest_data = json.loads(
                    handle.read("manifest.json").decode("utf-8")
                )

            files = manifest_data["files"]
            self.assertEqual(2, len(files))
            self.assertEqual("a.txt", files[0]["path"])
            self.assertEqual("nested/b.txt", files[1]["path"])
            self.assertEqual("alpha", (root / "a.txt").read_text())

    def test_zip_contents_match_manifest_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            manifest = [_build_entry(root, first), _build_entry(root, second)]

            data = b"".join(iter_archive(root, manifest))
            with zipfile.ZipFile(io.BytesIO(data), "r") as handle:
                manifest_data = json.loads(handle.read("manifest.json"))
                for entry in manifest_data["files"]:
                    file_bytes = handle.read(entry["path"])
                    self.assertEqual(hashlib.sha256(file_bytes).hexdigest(), entry["sha256"])

    def test_symlink_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "target.txt"
            source.write_text("target", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(source)
            manifest = [{"path": "link.txt", "size_bytes": 6, "sha256": "0" * 64, "media_type": "text/plain"}]

            with self.assertRaises(ArchiveError):
                b"".join(iter_archive(root, manifest))

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            manifest = [
                {"path": "../outside.txt", "size_bytes": 7, "sha256": "0" * 64, "media_type": "text/plain"}
            ]
            with self.assertRaises(ArchiveError):
                b"".join(iter_archive(root, manifest))

    def test_content_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("stable", encoding="utf-8")
            manifest = [
                {
                    "path": "target.txt",
                    "size_bytes": 10,
                    "sha256": hashlib.sha256(b"wrong length").hexdigest(),
                    "media_type": "text/plain",
                }
            ]
            with self.assertRaises(ArchiveChangedError):
                b"".join(iter_archive(root, manifest))

    def test_close_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("small", encoding="utf-8")
            manifest = [_build_entry(root, target)]
            events: list[str] = []

            class Lease:
                def renew(self) -> None:
                    events.append("renew")

                def release(self) -> None:
                    events.append("release")

                def cancel(self) -> None:
                    events.append("cancel")

            iterator = iter_archive(root, manifest, lease=Lease())
            output_iter = iter(iterator)
            next(output_iter)
            iterator.close()
            self.assertIn("cancel", events)
            self.assertIn("release", events)

    def test_close_releases_lease_and_stops_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("x" * 50000, encoding="utf-8")
            manifest = [_build_entry(root, target)]
            events: list[str] = []

            class Lease:
                def renew(self) -> None:
                    events.append("renew")

                def release(self) -> None:
                    events.append("release")

                def cancel(self) -> None:
                    events.append("cancel")

            iterator = _ArchiveIterator(root, manifest, chunk_size=1024, lease=Lease())
            iterator_thread = iterator._thread
            output_iter = iter(iterator)
            next(output_iter)
            iterator.close()
            self.assertFalse(iterator_thread.is_alive())
            self.assertIn("cancel", events)
            self.assertIn("release", events)

    def test_lease_renew_occurs_per_file_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("01234", encoding="utf-8")
            manifest = [_build_entry(root, target)]
            events: list[str] = []

            class Lease:
                def __init__(self) -> None:
                    self.renew_count = 0

                def renew(self) -> None:
                    self.renew_count += 1
                    events.append("renew")

                def release(self) -> None:
                    events.append("release")

                def cancel(self) -> None:
                    events.append("cancel")

            lease = Lease()
            b"".join(iter_archive(root, manifest, chunk_size=2, lease=lease))
            # 5-byte payload with chunk_size=2 => 3 chunks, plus one manifest write.
            self.assertEqual(4, lease.renew_count)
            self.assertIn("release", events)

    def test_manifest_entry_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one.txt"
            two = root / "two.txt"
            one.write_text("one", encoding="utf-8")
            two.write_text("two", encoding="utf-8")
            manifest = [_build_entry(root, one), _build_entry(root, two)]
            with self.assertRaises(ArchiveError):
                b"".join(iter_archive(root, manifest, max_entries=1))

    def test_total_bytes_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("0123456789", encoding="utf-8")
            manifest = [_build_entry(root, target)]
            with self.assertRaises(ArchiveError):
                b"".join(iter_archive(root, manifest, max_total_bytes=1))

    def test_deterministic_zip_entry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("content", encoding="utf-8")
            manifest = [_build_entry(root, target)]

            data = b"".join(iter_archive(root, manifest, chunk_size=4))
            with zipfile.ZipFile(io.BytesIO(data), "r") as handle:
                entry = handle.getinfo("target.txt")
                manifest_entry = handle.getinfo("manifest.json")
                self.assertEqual((1980, 1, 1, 0, 0, 0), entry.date_time)
                self.assertEqual((1980, 1, 1, 0, 0, 0), manifest_entry.date_time)
                self.assertEqual(zipfile.ZIP_DEFLATED, entry.compress_type)
                self.assertEqual(zipfile.ZIP_DEFLATED, manifest_entry.compress_type)
                self.assertEqual(0o100644 << 16, entry.external_attr)
                self.assertEqual(0o100644 << 16, manifest_entry.external_attr)
                manifest_payload = handle.read("manifest.json").decode("utf-8")
                normalized = json.dumps(
                    json.loads(manifest_payload),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.assertEqual(normalized, manifest_payload)

    def test_zip_info_has_expected_defaults(self) -> None:
        info = _build_zip_info("example.txt", file_size=123)
        self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
        self.assertEqual(zipfile.ZIP_DEFLATED, info.compress_type)
        self.assertEqual(0x800, info.flag_bits & 0x800)
        self.assertEqual(0o100644 << 16, info.external_attr)
        self.assertEqual(123, info.file_size)


if __name__ == "__main__":
    unittest.main()
