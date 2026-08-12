from __future__ import annotations

import io
import html
import hashlib
import copy
import json
import multiprocessing
import os
import stat
import tempfile
import sys
import unittest
import urllib.error
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402
import formula_only_second_pass as formula_second_pass  # noqa: E402
import semantic_reflow  # noqa: E402
import vlm_full_dir_review as vlm_review  # noqa: E402


def _hold_qpa_job_lock(
    output_root: str,
    job_name: str,
    ready: object,
    release: object,
    result_queue: object,
) -> None:
    try:
        with adapter._PersistentJobLock(Path(output_root), job_name):
            ready.set()
            release.wait(10)
        result_queue.put(None)
    except Exception as exc:  # pragma: no cover - returned to the parent process
        result_queue.put((exc.__class__.__name__, str(exc)))


def _qpa_lifecycle_args(
    input_file: Path,
    output_root: Path,
    *,
    job_id: str = "job-1",
    expected_sha256: str | None = None,
) -> Namespace:
    argv = [
        "quality_parity_adapter.py",
        "--input-file",
        str(input_file),
        "--output-root",
        str(output_root),
        "--job-id",
        job_id,
        "--ocr-fallback-policy",
        "off",
    ]
    if expected_sha256 is not None:
        argv.extend(["--expected-input-sha256", expected_sha256])
    with patch.object(sys, "argv", argv):
        return adapter.parse_args()


def _write_visible_test_png(
    path: Path,
    size: tuple[int, int] = (48, 32),
    *,
    background: str = "white",
) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    draw.line((2, 2, size[0] - 3, size[1] - 3), fill="black", width=3)
    draw.line((2, size[1] - 3, size[0] - 3, 2), fill="black", width=2)
    image.save(path)


def _write_horizontal_rule_test_png(path: Path, size: tuple[int, int] = (80, 12)) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    middle = size[1] // 2
    draw.line((2, middle, size[0] - 3, middle), fill="black", width=2)
    image.save(path)


def _write_formula_text_test_png(path: Path, size: tuple[int, int] = (120, 20)) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((4, 3), "x = y + z", fill="black")
    image.save(path)


def _ruled_grid_nested_fixture() -> tuple[bytes, dict[str, object]]:
    """Build a 5x5 ruled fixture with To colspan=3 and From rowspan=3."""

    from PIL import Image, ImageDraw

    size = 500
    lines = [10, 110, 210, 310, 410, 490]
    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    for coordinate in lines:
        draw.line((lines[0], coordinate, lines[-1], coordinate), fill="black", width=3)
    for coordinate in lines:
        draw.line((coordinate, lines[0], coordinate, lines[-1]), fill="black", width=3)
    # To spans columns 2..4 on the first row; From spans rows 2..4 in column 0.
    for coordinate in (310, 410):
        draw.line((coordinate, lines[0] + 2, coordinate, lines[1] - 2), fill="white", width=7)
    for coordinate in (310, 410):
        draw.line((lines[0] + 2, coordinate, lines[1] - 2, coordinate), fill="white", width=7)

    text_positions = [
        ("To", (350, 60)),
        ("Solid", (260, 160)),
        ("Liquid", (360, 160)),
        ("Gas", (450, 160)),
        ("From", (60, 350)),
        ("Solid", (160, 260)),
        ("Solid trans", (260, 260)),
        ("Melting", (360, 260)),
        ("Sublimation", (450, 260)),
        ("Liquid", (160, 360)),
        ("Freezing", (260, 360)),
        ("Boiling", (450, 360)),
        ("Gas", (160, 460)),
        ("Deposition", (260, 460)),
        ("Condensation", (360, 460)),
    ]
    for text, (center_x, center_y) in text_positions:
        draw.text((center_x - 18, center_y - 7), text, fill="black")
    # The retry OCR intentionally omits these two visible literal dashes.
    draw.line((352, 360, 368, 360), fill="black", width=2)
    draw.line((442, 450, 458, 450), fill="black", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    text_nodes = []
    for text, (center_x, center_y) in text_positions:
        text_nodes.append(
            {
                "label": "text",
                "text": text,
                "prov": [{
                    "page_no": 1,
                    "bbox": {
                        "l": center_x - 18,
                        "r": center_x + 18,
                        "t": center_y - 7,
                        "b": center_y + 7,
                        "coord_origin": "TOPLEFT",
                    },
                }],
            }
        )
    response = {
        "status": "success",
        "document": {
            "json_content": {
                "pages": {"1": {"size": {"width": size, "height": size}}},
                "texts": text_nodes,
            }
        },
    }
    return buffer.getvalue(), response


def _ruled_grid_sparse_chart_fixture() -> tuple[bytes, dict[str, object]]:
    from PIL import Image, ImageDraw

    size = 300
    # Reviewer repro: only three horizontal/vertical rules make a 2x2 chart
    # with four labels.  Every cell is covered, so coverage alone must not
    # make this visual box look like a recovered semantic table.
    lines = [10, 150, 290]
    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    for coordinate in lines:
        draw.line((lines[0], coordinate, lines[-1], coordinate), fill="black", width=3)
        draw.line((coordinate, lines[0], coordinate, lines[-1]), fill="black", width=3)
    labels = [("SeriesA", 80, 80), ("SeriesB", 220, 80), ("x1", 80, 220), ("x2", 220, 220)]
    for text, center_x, center_y in labels:
        draw.text((center_x - 8, center_y - 7), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    response = {
        "status": "success",
        "document": {
            "json_content": {
                "pages": {"1": {"size": {"width": size, "height": size}}},
                "texts": [
                    {
                        "label": "text",
                        "text": text,
                        "prov": [{
                            "page_no": 1,
                            "bbox": {
                                "l": center_x - 8,
                                "r": center_x + 8,
                                "t": center_y - 7,
                                "b": center_y + 7,
                                "coord_origin": "TOPLEFT",
                            },
                        }],
                    }
                    for text, center_x, center_y in labels
                ],
            }
        },
    }
    return buffer.getvalue(), response


def _ruled_grid_full_chart_fixture() -> tuple[bytes, dict[str, object]]:
    """A fully labelled 3x3 chart with no nested/merged semantics."""

    from PIL import Image, ImageDraw

    size = 300
    # Keep a safe 40px padding around the grid; detector must validate its own
    # span/intersections rather than require rules to touch the crop edge.
    lines = [40, 120, 200, 280]
    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    for coordinate in lines:
        draw.line((lines[0], coordinate, lines[-1], coordinate), fill="black", width=3)
        draw.line((coordinate, lines[0], coordinate, lines[-1]), fill="black", width=3)
    text_nodes = []
    for row in range(3):
        for col in range(3):
            center_x = (lines[col] + lines[col + 1]) // 2
            center_y = (lines[row] + lines[row + 1]) // 2
            text = f"Series{row}{col}"
            draw.text((center_x - 24, center_y - 7), text, fill="black")
            text_nodes.append(
                {
                    "label": "text",
                    "text": text,
                    "prov": [{
                        "page_no": 1,
                        "bbox": {
                            "l": center_x - 24,
                            "r": center_x + 24,
                            "t": center_y - 7,
                            "b": center_y + 7,
                            "coord_origin": "TOPLEFT",
                        },
                    }],
                }
            )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), {
        "status": "success",
        "document": {
            "json_content": {
                "pages": {"1": {"size": {"width": size, "height": size}}},
                "texts": text_nodes,
            }
        },
    }


def _formula_test_node(text: str, *, page_no: int = 1) -> dict[str, object]:
    return {
        "label": "formula",
        "text": text,
        "prov": [
            {
                "page_no": page_no,
                "bbox": {
                    "l": 5.0,
                    "r": 95.0,
                    "t": 95.0,
                    "b": 5.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            }
        ],
    }


def _formula_test_crop_diagnostic(
    output_dir: Path,
    index: int,
    formula: dict[str, object],
    *,
    source_pdf_sha256: str = "a" * 64,
) -> dict[str, object]:
    prov = adapter.first_prov(formula) or {}
    bbox = adapter.bbox_geometry(prov)
    page_no = int(prov.get("page_no") or 0)
    if bbox is None or page_no <= 0:
        raise AssertionError("formula fixture requires verified geometry")
    identity_sha256 = adapter._formula_content_identity_sha256(
        str(formula.get("text") or "")
    )
    result: dict[str, object] = {
        "index": index,
        "page_no": page_no,
        "bbox": dict(bbox),
        "source_pdf_sha256": source_pdf_sha256,
        "formula_content_identity_sha256": identity_sha256,
    }
    for kind, suffix in (("source", ""), ("context", "_context")):
        asset = output_dir / "formulas" / f"formula_{index}{suffix}.png"
        if not asset.is_file():
            continue
        from PIL import Image

        with Image.open(asset) as image:
            width, height = image.size
        result[kind] = {
            "path": f"formulas/formula_{index}{suffix}.png",
            "page_no": page_no,
            "bbox": dict(bbox),
            "page_size": {"width": 100.0, "height": 100.0},
            "pixel_width": width,
            "pixel_height": height,
            "asset_sha256": adapter.file_sha256(asset),
            "source_pdf_sha256": source_pdf_sha256,
            "formula_content_identity_sha256": identity_sha256,
        }
    return result


class _JsonResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class QpaInputLifecycleTests(unittest.TestCase):
    def test_expected_input_sha256_argument_is_strict(self) -> None:
        digest = "A" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_file = root / "input.pdf"
            input_file.write_bytes(b"pdf")
            parsed = _qpa_lifecycle_args(
                input_file,
                root / "out",
                expected_sha256=digest,
            )
        self.assertEqual(parsed.expected_input_sha256, digest.lower())

        with patch.object(
            sys,
            "argv",
            [
                "quality_parity_adapter.py",
                "--input-file",
                "input.pdf",
                "--output-root",
                "out",
                "--expected-input-sha256",
                "not-a-sha",
            ],
        ), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            adapter.parse_args()

    def test_persistent_job_lock_reuses_stale_inode_and_blocks_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "out"
            with adapter._PersistentJobLock(output_root, "job-1") as first:
                lock_path = first.lock_path
            self.assertIsNotNone(lock_path)
            assert lock_path is not None
            stale_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)

            context = multiprocessing.get_context("fork")
            ready = context.Event()
            release = context.Event()
            result_queue = context.Queue()
            process = context.Process(
                target=_hold_qpa_job_lock,
                args=(str(output_root), "job-1", ready, release, result_queue),
            )
            process.start()
            self.assertTrue(ready.wait(5), "child did not acquire the job lock")
            try:
                with self.assertRaises(adapter._InputLifecycleError) as caught:
                    with adapter._PersistentJobLock(output_root, "job-1"):
                        pass
                self.assertEqual(caught.exception.reason, "job_lock_already_held")
                with adapter._PersistentJobLock(output_root, "job-2"):
                    pass
            finally:
                release.set()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertIsNone(result_queue.get(timeout=2))
            self.assertEqual(
                (lock_path.stat().st_dev, lock_path.stat().st_ino),
                stale_identity,
            )
            with adapter._PersistentJobLock(output_root, "job-1"):
                pass

    def test_active_job_lock_detects_unlink_and_replacement_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "out"
            with adapter._PersistentJobLock(output_root, "job-1") as lock:
                assert lock.lock_path is not None
                lock.lock_path.unlink()
                lock.lock_path.write_text("replacement", encoding="utf-8")
                with self.assertRaises(adapter._InputLifecycleError) as caught:
                    lock.verify()
                self.assertEqual(caught.exception.reason, "job_lock_entry_replaced")

    def test_claim_does_not_adopt_directory_replaced_before_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "job"
            moved_claim = root / "moved-claim"
            attacker = root / "attacker"
            attacker.mkdir()
            marker = attacker / "marker.txt"
            marker.write_text("preserve", encoding="utf-8")
            guard = adapter._FreshOutputGuard(output_dir)
            original_claim = adapter._FreshOutputGuard.claim

            def replace_before_claim(
                current_guard: adapter._FreshOutputGuard,
                directory_fd: int,
                identity: tuple[int, int],
            ) -> None:
                output_dir.rename(moved_claim)
                attacker.rename(output_dir)
                original_claim(current_guard, directory_fd, identity)

            with patch.object(
                adapter._FreshOutputGuard,
                "claim",
                side_effect=replace_before_claim,
                autospec=True,
            ), self.assertRaises(adapter._InputLifecycleError) as caught:
                adapter._claim_fresh_output_directory(output_dir, guard)
            self.assertEqual(caught.exception.reason, "job_output_directory_replaced")
            cleanup = guard.cleanup()
            self.assertFalse(cleanup["output_dir_removed"])
            self.assertEqual((output_dir / "marker.txt").read_text(), "preserve")

    def test_lock_symlink_and_hardlink_targets_are_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_root = root / "out"
            output_root.mkdir()
            lock_root = output_root / adapter.JOB_LOCK_DIRECTORY_NAME
            lock_root.mkdir(mode=0o700)
            lock_name = hashlib.sha256(b"job-1").hexdigest() + ".lock"
            sentinel = root / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            (lock_root / lock_name).symlink_to(sentinel)
            with self.assertRaises(adapter._InputLifecycleError):
                with adapter._PersistentJobLock(output_root, "job-1"):
                    pass
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

            (lock_root / lock_name).unlink()
            os.link(sentinel, lock_root / lock_name)
            with self.assertRaises(adapter._InputLifecycleError) as caught:
                with adapter._PersistentJobLock(output_root, "job-1"):
                    pass
            self.assertEqual(caught.exception.reason, "job_lock_hardlink_not_allowed")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

            external_root = root / "external-root"
            external_root.mkdir()
            symlink_root = root / "output-root-link"
            symlink_root.symlink_to(external_root, target_is_directory=True)
            with self.assertRaises(adapter._InputLifecycleError) as caught:
                with adapter._PersistentJobLock(symlink_root, "job-2"):
                    pass
            self.assertEqual(caught.exception.reason, "output_root_symlink_not_allowed")

    def test_snapshot_rejects_symlink_and_nonregular_input_without_opening_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"submitted-pdf")
            symlink = root / "link.pdf"
            symlink.symlink_to(source)
            fifo = root / "input.fifo"
            os.mkfifo(fifo)
            with adapter._PersistentJobLock(root / "out", "job") as lock:
                for path, expected in (
                    (symlink, "input_symlink_not_allowed"),
                    (fifo, "input_not_regular_file"),
                ):
                    with self.subTest(path=path.name):
                        with self.assertRaises(adapter._InputLifecycleError) as caught:
                            adapter._create_immutable_input_snapshot(path, lock, None)
                        self.assertEqual(caught.exception.reason, expected)

                raced = root / "raced.pdf"
                raced.write_bytes(b"regular-before-open")
                real_open = os.open
                swapped = False

                def replace_with_fifo_before_open(
                    path: object,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal swapped
                    if dir_fd is None and Path(path) == raced and not swapped:
                        swapped = True
                        raced.unlink()
                        os.mkfifo(raced)
                    if dir_fd is None:
                        return real_open(path, flags, mode)
                    return real_open(path, flags, mode, dir_fd=dir_fd)

                with patch.object(
                    adapter.os,
                    "open",
                    side_effect=replace_with_fifo_before_open,
                ):
                    with self.assertRaises(adapter._InputLifecycleError) as caught:
                        adapter._create_immutable_input_snapshot(raced, lock, None)
                self.assertEqual(caught.exception.reason, "input_not_regular_file")

    def test_snapshot_enforces_expected_hash_dynamic_cap_and_read_only_fd(self) -> None:
        payload = b"submitted-pdf-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(payload)
            with adapter._PersistentJobLock(root / "out", "job") as lock:
                with self.assertRaises(adapter._InputLifecycleError) as caught:
                    adapter._create_immutable_input_snapshot(source, lock, "0" * 64)
                self.assertEqual(caught.exception.reason, "expected_input_sha256_mismatch")

                snapshot = adapter._create_immutable_input_snapshot(source, lock, digest)
                snapshot_root = snapshot.root_path
                try:
                    self.assertEqual(snapshot.read_bytes(), payload)
                    self.assertEqual(snapshot.sha256, digest)
                    with self.assertRaises(OSError):
                        os.write(snapshot.fd, b"x")
                finally:
                    snapshot.cleanup()
                self.assertFalse(snapshot_root.exists())

                with patch.object(adapter, "INPUT_SNAPSHOT_MAX_BYTES", len(payload) - 1):
                    with self.assertRaises(adapter._InputLifecycleError) as caught:
                        adapter._create_immutable_input_snapshot(source, lock, None)
                self.assertEqual(
                    caught.exception.reason,
                    "input_exceeds_256_mib_snapshot_limit",
                )

    def test_snapshot_initial_verify_failure_cleans_private_temp_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"pdf")
            snapshot_root = root / "private-snapshot"
            with adapter._PersistentJobLock(root / "out", "job") as lock, patch.object(
                adapter.tempfile,
                "mkdtemp",
                side_effect=lambda **_kwargs: str(
                    snapshot_root.mkdir(mode=0o700) or snapshot_root
                ),
            ), patch.object(
                adapter._ImmutableInputSnapshot,
                "verify",
                side_effect=adapter._InputLifecycleError("forced_verify_failure"),
            ):
                with self.assertRaises(adapter._InputLifecycleError):
                    adapter._create_immutable_input_snapshot(source, lock, None)
            self.assertFalse(snapshot_root.exists())

    def test_snapshot_rejects_runtime_growth_and_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"abcd")
            with adapter._PersistentJobLock(root / "out", "job") as lock:
                real_read = os.read
                grew = False

                def read_then_grow(fd: int, size: int) -> bytes:
                    nonlocal grew
                    chunk = real_read(fd, size)
                    if chunk and not grew:
                        grew = True
                        with source.open("ab") as handle:
                            handle.write(b"e")
                    return chunk

                with patch.object(
                    adapter, "INPUT_SNAPSHOT_MAX_BYTES", 4
                ), patch.object(adapter.os, "read", side_effect=read_then_grow):
                    with self.assertRaises(adapter._InputLifecycleError) as caught:
                        adapter._create_immutable_input_snapshot(source, lock, None)
                self.assertEqual(
                    caught.exception.reason,
                    "input_exceeds_256_mib_snapshot_limit",
                )

                source.write_bytes(b"abcd")
                replaced = False

                def read_then_replace(fd: int, size: int) -> bytes:
                    nonlocal replaced
                    chunk = real_read(fd, size)
                    if chunk and not replaced:
                        replaced = True
                        replacement = root / "replacement.pdf"
                        replacement.write_bytes(b"abcd")
                        os.replace(replacement, source)
                    return chunk

                with patch.object(adapter.os, "read", side_effect=read_then_replace):
                    with self.assertRaises(adapter._InputLifecycleError) as caught:
                        adapter._create_immutable_input_snapshot(source, lock, None)
                self.assertEqual(caught.exception.reason, "input_changed_during_snapshot")

    def test_persisted_source_is_bound_to_original_inode_and_hash(self) -> None:
        payload = b"persistent-source"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.pdf"
            source.write_bytes(payload)
            output_root = root / "out"
            output_dir = output_root / "job"
            with adapter._PersistentJobLock(output_root, "job") as lock:
                guard = adapter._FreshOutputGuard(output_dir)
                adapter._claim_fresh_output_directory(output_dir, guard)
                snapshot = adapter._create_immutable_input_snapshot(source, lock, None)
                try:
                    published = adapter._persist_job_source(snapshot, output_dir, guard)
                    self.assertEqual(published.read_bytes(), payload)
                    self.assertTrue(stat.S_ISREG(published.lstat().st_mode))
                    self.assertEqual(published.stat().st_mode & 0o777, 0o400)
                    expected_identity = guard.source_identity
                    replacement = output_dir / "replacement.pdf"
                    replacement.write_bytes(payload)
                    os.chmod(replacement, 0o400)
                    os.replace(replacement, published)
                    with self.assertRaises(adapter._InputLifecycleError) as caught:
                        adapter._verify_job_source(
                            snapshot,
                            published,
                            expected_identity=expected_identity,
                        )
                    self.assertEqual(
                        caught.exception.reason,
                        "job_source_pdf_identity_mismatch",
                    )
                finally:
                    snapshot.cleanup()
                    guard.cleanup()

    def test_main_expected_hash_mismatch_and_unreachable_server_clean_partial_output(self) -> None:
        payload = b"submitted"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.pdf"
            source.write_bytes(payload)
            output_root = root / "out"

            mismatch_args = _qpa_lifecycle_args(
                source,
                output_root,
                expected_sha256="0" * 64,
            )
            with patch.object(adapter, "parse_args", return_value=mismatch_args), patch.object(
                adapter, "get_json"
            ) as get_json_mock, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(adapter.main(), 2)
            get_json_mock.assert_not_called()
            self.assertFalse((output_root / "job-1").exists())

            missing = root / "missing.pdf"
            missing_args = _qpa_lifecycle_args(missing, output_root)
            with patch.object(adapter, "parse_args", return_value=missing_args), patch.object(
                adapter, "get_json"
            ) as missing_get_json, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(adapter.main(), 2)
            missing_get_json.assert_not_called()
            self.assertFalse((output_root / "job-1").exists())

            unreachable_args = _qpa_lifecycle_args(
                source,
                output_root,
                expected_sha256=digest,
            )
            with patch.object(adapter, "parse_args", return_value=unreachable_args), patch.object(
                adapter,
                "pdf_text_layer_profile",
                return_value={"page_count": 1, "image_only_candidate": False},
            ), patch.object(
                adapter,
                "get_json",
                side_effect=urllib.error.URLError("offline"),
            ), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(adapter.main(), 2)
            self.assertFalse((output_root / "job-1").exists())

    def test_main_fresh_preflight_precedes_network_and_preserves_retained_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.pdf"
            source.write_bytes(b"pdf")
            output_root = root / "out"
            retained = output_root / "job-1"
            retained.mkdir(parents=True)
            sentinel = retained / "status.json"
            sentinel.write_text('{"ok":true}', encoding="utf-8")
            args = _qpa_lifecycle_args(source, output_root)
            with patch.object(adapter, "parse_args", return_value=args), patch.object(
                adapter, "get_json"
            ) as get_json_mock, patch.object(
                adapter, "run_conversion"
            ) as conversion_mock, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(adapter.main(), 2)
            get_json_mock.assert_not_called()
            conversion_mock.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"ok":true}')

    def test_contract_write_rejects_late_symlink_without_touching_sentinel(self) -> None:
        response = {
            "document": {
                "md_content": "body",
                "html_content": "<p>body</p>",
                "json_content": {},
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "job"
            guard = adapter._FreshOutputGuard(output_dir)
            adapter._claim_fresh_output_directory(output_dir, guard)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            (output_dir / "document.md").symlink_to(sentinel)
            with self.assertRaises(adapter._InputLifecycleError) as caught:
                adapter.write_contract_outputs(
                    output_dir,
                    response,
                    {},
                    {},
                    output_guard=guard,
                )
            self.assertEqual(caught.exception.reason, "unsafe_contract_output_target")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            guard.cleanup()

    def test_phase_tree_gate_rejects_late_hardlink_before_mutator_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "job"
            guard = adapter._FreshOutputGuard(output_dir)
            adapter._claim_fresh_output_directory(output_dir, guard)
            sentinel = root / "sentinel.html"
            sentinel.write_text("unchanged", encoding="utf-8")
            os.link(sentinel, output_dir / "document.html")
            with self.assertRaises(adapter._InputLifecycleError) as caught:
                adapter._assert_safe_owned_output_tree(guard)
            self.assertEqual(caught.exception.reason, "unsafe_job_output_tree_entry")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            cleanup = guard.cleanup()
            self.assertTrue(cleanup["output_dir_removed"])

    def test_main_rejects_child_symlink_injected_after_claim(self) -> None:
        response = {
            "document": {
                "md_content": "body",
                "html_content": "<p>body</p>",
                "json_content": {},
            }
        }
        metadata = {
            "text_quality_gxx_count": 0,
            "text_quality_gxx_density": 0.0,
        }
        status = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.pdf"
            source.write_bytes(b"pdf")
            output_root = root / "out"
            output_dir = output_root / "job-1"
            sentinel = root / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            args = _qpa_lifecycle_args(source, output_root)

            def inject_after_claim(_url: str) -> dict[str, str]:
                self.assertTrue(output_dir.is_dir())
                (output_dir / "document.md").symlink_to(sentinel)
                return {"version": "test"}

            with patch.object(adapter, "parse_args", return_value=args), patch.object(
                adapter,
                "pdf_text_layer_profile",
                return_value={"page_count": 1, "image_only_candidate": False},
            ), patch.object(
                adapter, "get_json", side_effect=inject_after_claim
            ), patch.object(
                adapter,
                "run_conversion",
                return_value=(response, metadata, status),
            ), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(adapter.main(), 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse(output_dir.exists())

    def test_main_uses_one_snapshot_for_all_phases_and_publishes_source_contract(self) -> None:
        payload = b"same-submitted-pdf-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        response = {
            "document": {
                "md_content": "body",
                "html_content": "<p>body</p>",
                "json_content": {"pages": {}, "texts": []},
            }
        }
        metadata = {
            "text_quality_gxx_count": 0,
            "text_quality_gxx_density": 0.0,
            "generated_outputs": [],
        }
        status = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        seen: list[tuple[str, Path, bytes]] = []

        def record_args(label: str, args: Namespace) -> None:
            seen.append((label, args.input_file, args._input_snapshot.read_bytes()))

        def fake_conversion(args: Namespace, _name: str, **_kwargs: object):
            record_args("conversion", args)
            source.write_bytes(b"changed-after-snapshot")
            return response, dict(metadata), json.loads(json.dumps(status))

        def fake_restore(
            _output: Path,
            _response: dict[str, object],
            _metadata: dict[str, object],
            _status: dict[str, object],
            args: Namespace,
        ) -> None:
            record_args("visual", args)

        def fake_portable_ocr(
            _output: Path,
            _metadata: dict[str, object],
            _status: dict[str, object],
            args: Namespace,
        ) -> None:
            record_args("portable-ocr", args)

        def fake_formula_second_pass(
            _output: Path,
            _metadata: dict[str, object],
            _status: dict[str, object],
            args: Namespace,
        ) -> None:
            record_args("formula-second-pass", args)

        def fake_semantic(
            _output: Path,
            _document: dict[str, object],
            source_path: Path,
            _metadata: dict[str, object],
            _status: dict[str, object],
        ) -> None:
            seen.append(("semantic", source_path, source_path.read_bytes()))

        def fake_finalize(
            _output: Path,
            _document: dict[str, object],
            semantic_path: Path,
            visual_path: Path,
            _metadata: dict[str, object],
            _status: dict[str, object],
            _args: Namespace,
            **_kwargs: object,
        ) -> dict[str, object]:
            seen.append(("final-semantic", semantic_path, semantic_path.read_bytes()))
            seen.append(("final-visual", visual_path, visual_path.read_bytes()))
            return {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.pdf"
            source.write_bytes(payload)
            output_root = root / "out"
            args = _qpa_lifecycle_args(
                source,
                output_root,
                expected_sha256=digest,
            )
            stdout = io.StringIO()
            with patch.object(adapter, "parse_args", return_value=args), patch.object(
                adapter,
                "pdf_text_layer_profile",
                return_value={"page_count": 1, "image_only_candidate": False},
            ), patch.object(
                adapter, "get_json", return_value={"version": "test"}
            ), patch.object(
                adapter, "run_conversion", side_effect=fake_conversion
            ), patch.object(
                adapter, "restore_review_artifact_layer", side_effect=fake_restore
            ), patch.object(
                adapter, "run_portable_formula_ocr", side_effect=fake_portable_ocr
            ), patch.object(
                adapter,
                "run_optional_formula_second_pass_safely",
                side_effect=fake_formula_second_pass,
            ), patch.object(
                adapter, "rebuild_semantic_surfaces", side_effect=fake_semantic
            ), patch.object(
                adapter, "_finalize_delivery_surfaces", side_effect=fake_finalize
            ), patch.object(
                adapter, "refresh_final_broken_local_refs", return_value=None
            ), patch.object(
                adapter, "find_text_layer_recovery_source"
            ) as sibling_mock, patch("sys.stdout", new=stdout):
                self.assertEqual(adapter.main(), 0)

            sibling_mock.assert_not_called()
            output_dir = output_root / "job-1"
            source_pdf = output_dir / "source.pdf"
            self.assertEqual(source_pdf.read_bytes(), payload)
            self.assertEqual(source_pdf.stat().st_mode & 0o777, 0o400)
            final_metadata = json.loads(
                (output_dir / "metadata.json").read_text(encoding="utf-8")
            )
            final_status = json.loads(
                (output_dir / "status.json").read_text(encoding="utf-8")
            )
            for key in (
                "input_sha256",
                "original_input_sha256",
                "conversion_input_sha256",
                "visual_evidence_input_sha256",
            ):
                self.assertEqual(final_metadata[key], digest)
            for key in (
                "input_file",
                "original_input_file",
                "conversion_input_file",
                "visual_evidence_input_file",
            ):
                self.assertEqual(final_metadata[key], "source.pdf")
            self.assertEqual(final_metadata["source_pdf"], "source.pdf")
            self.assertEqual(final_metadata["output_dir"], ".")
            self.assertEqual(final_metadata["metadata_path"], "metadata.json")
            self.assertEqual(final_status["output_dir"], ".")
            self.assertEqual(final_status["status_path"], "status.json")
            self.assertEqual(
                final_metadata["text_layer_recovery"]["reason"],
                "automatic_sibling_text_layer_recovery_disabled",
            )
            self.assertTrue(
                final_metadata["fresh_output_preflight"]["contract_files_written"]
            )
            self.assertTrue(final_status["ok"])
            self.assertEqual({item[2] for item in seen}, {payload})
            snapshot_paths = {item[1] for item in seen}
            self.assertEqual(len(snapshot_paths), 1)
            snapshot_path = next(iter(snapshot_paths))
            self.assertNotEqual(snapshot_path, source)
            self.assertFalse(snapshot_path.exists())
            self.assertNotIn("quality-parity-input-", json.dumps(final_metadata))

    def test_main_runtime_failure_releases_lock_and_removes_owned_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.pdf"
            source.write_bytes(b"pdf")
            output_root = root / "out"
            args = _qpa_lifecycle_args(source, output_root)
            with patch.object(adapter, "parse_args", return_value=args), patch.object(
                adapter,
                "pdf_text_layer_profile",
                return_value={"page_count": 1, "image_only_candidate": False},
            ), patch.object(
                adapter, "get_json", return_value={"version": "test"}
            ), patch.object(
                adapter, "run_conversion", side_effect=RuntimeError("conversion failed")
            ), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(adapter.main(), 1)
            self.assertFalse((output_root / "job-1").exists())
            with adapter._PersistentJobLock(output_root, "job-1"):
                pass


class DoclingHttpRetryTests(unittest.TestCase):
    def test_recovery_guidance_is_deployment_neutral(self) -> None:
        self.assertNotIn("/Users/", adapter.START_COMMAND)
        self.assertIn("DOCKER.md", adapter.START_COMMAND)
        self.assertIn("MACOS.md", adapter.START_COMMAND)

    def test_wrapped_transient_http_error_remains_classifiable(self) -> None:
        transient = urllib.error.HTTPError(
            "http://127.0.0.1:5001/v1/convert/source",
            503,
            "Unavailable",
            {},
            None,
        )
        wrapped = RuntimeError("Docling Serve HTTP 503")
        wrapped.__cause__ = transient

        self.assertTrue(adapter.is_transient_http_error(wrapped))

    def test_result_visibility_404_is_retried(self) -> None:
        url = "http://127.0.0.1:5001/v1/convert/source"
        pending = urllib.error.HTTPError(
            url,
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"detail":"Task result not found. Please wait."}'),
        )
        with (
            patch.object(
                adapter.urllib.request,
                "urlopen",
                side_effect=[pending, _JsonResponse({"status": "success"})],
            ) as urlopen,
            patch.object(adapter.time, "sleep") as sleep,
        ):
            result = adapter.post_json(
                url,
                {"sources": []},
                timeout=10,
                retries=1,
                retry_sleep_seconds=0.01,
            )

        self.assertEqual(result, {"status": "success"})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_unrelated_404_is_not_retried(self) -> None:
        url = "http://127.0.0.1:5001/v1/convert/source"
        missing = urllib.error.HTTPError(
            url,
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"detail":"unknown endpoint"}'),
        )
        with patch.object(adapter.urllib.request, "urlopen", side_effect=missing) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "unknown endpoint"):
                adapter.post_json(
                    url,
                    {"sources": []},
                    timeout=10,
                    retries=2,
                    retry_sleep_seconds=0.01,
                )

        self.assertEqual(urlopen.call_count, 1)


class VlmWorkerPreflightTests(unittest.TestCase):
    def test_supported_worker_can_import_docling(self) -> None:
        completed = Namespace(returncode=0, stdout="", stderr="")
        with patch.object(vlm_review.subprocess, "run", return_value=completed) as run:
            error = vlm_review.validate_worker_python("/tmp/python3.12")

        self.assertIsNone(error)
        run.assert_called_once_with(
            ["/tmp/python3.12", "-c", "import docling"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_missing_docling_fails_preflight_before_batch(self) -> None:
        completed = Namespace(
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'docling'",
        )
        with patch.object(vlm_review.subprocess, "run", return_value=completed):
            error = vlm_review.validate_worker_python("python3")

        self.assertIn("python3", error or "")
        self.assertIn("No module named 'docling'", error or "")

    def test_main_stops_before_batch_when_worker_preflight_fails(self) -> None:
        args = Namespace(worker_pdf=None, python="python3")
        with (
            patch.object(vlm_review, "parse_args", return_value=args),
            patch.object(
                vlm_review,
                "validate_worker_python",
                return_value="python3: No module named 'docling'",
            ),
            patch.object(vlm_review, "run_batch") as run_batch,
            patch.object(vlm_review.sys, "stderr", io.StringIO()) as stderr,
        ):
            result = vlm_review.main()

        self.assertEqual(result, 2)
        self.assertIn("Worker Python preflight failed", stderr.getvalue())
        run_batch.assert_not_called()


class FinalSurfaceStatusTests(unittest.TestCase):
    def test_formula_fallback_css_selector_is_not_counted_as_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                "<style>.formula-tex-fallback{font-family:math}</style>"
                '<div class="formula"><math><mi>x</mi></math></div>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("$$\nx\n$$\n", encoding="utf-8")
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {"counts": {"formulas": 1}},
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 1,
                        "formula_source_markdown_ref_count": 1,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, metadata, status
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["html_tex_fallback_count"], 0)

    def test_unrenderable_formula_surface_fails_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                '<div class="formula formula-tex-fallback"><code>x</formula</code></div>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\nx</formula\n$$\n", encoding="utf-8"
            )
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {"counts": {"formulas": 1}}
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, metadata, status
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["html_math_count"], 0)
        # The raw token inside HTML <code> is literal code content; only the
        # Markdown formula surface contributes an undecoded model token.
        self.assertEqual(result["raw_model_token_count"], 1)
        self.assertFalse(status["ok"])
        self.assertEqual(status["success_class"], "degraded_failure")

    def test_partial_formula_coverage_fails_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text(
                "$$x$$\n", encoding="utf-8"
            )
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {"counts": {"formulas": 2}}
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, metadata, status
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "incomplete_formula_source_visual_coverage", result["failure_reasons"]
        )
        self.assertNotIn("incomplete_mathml_coverage", result["failure_reasons"])
        self.assertNotIn(
            "incomplete_markdown_math_coverage", result["failure_reasons"]
        )

    def test_formula_coverage_uses_raw_formula_count_and_warns_when_source_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {
                        "texts": [
                            {"label": "formula", "text": r"x = y \quad (1)"},
                            {"label": "formula", "text": r"z = q \quad (2)"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x = y \\quad (1)$$\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": False,
                        "applied": True,
                        "counts": {"formulas": 1},
                        "authoritative_surfaces": ["document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 2,
                        "formula_source_markdown_ref_count": 2,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["machine_surface_ok"])
        self.assertEqual(status["success_class"], "degraded_success")
        self.assertEqual(result["formula_count"], 2)
        self.assertEqual(
            set(result["warning_reasons"]),
            {"incomplete_mathml_coverage", "incomplete_markdown_math_coverage"},
        )
        self.assertTrue(result["source_visual_authoritative"])
        self.assertIn("machine_formula_surface_warning:incomplete_mathml_coverage", status["warnings"])

    def test_formula_visual_shortfall_fails_quality_gate_even_with_machine_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {"texts": [{"label": "formula", "text": r"x = y \quad (1)"}]},
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": False,
                        "applied": True,
                        "counts": {"formulas": 1},
                        "authoritative_surfaces": ["document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 0,
                        "formula_source_markdown_ref_count": 0,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "incomplete_formula_source_visual_coverage",
            result["failure_reasons"],
        )

    def test_validate_final_formula_surfaces_fails_unrepaired_flattened_inline_math_vacuous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text("", encoding="utf-8")
            (output_dir / "document.md").write_text("C ∈ R H\\n", encoding="utf-8")
            (output_dir / "document.json").write_text(
                adapter.json.dumps({"texts": []}),
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"formulas": 0, "inline_math_repairs": 0},
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 0,
                        "formula_source_markdown_ref_count": 0,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["formula_count"], 0)
        self.assertEqual(result["inline_math_residuals"], ["real_vector_space_script_lost"])
        self.assertIn("unrepaired_inline_math_residuals", result["failure_reasons"])

    def test_validate_final_formula_surfaces_allows_repaired_flattened_inline_math(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text("", encoding="utf-8")
            (output_dir / "document.md").write_text(
                "C ∈ ℝ^{H} and W ∈ ℝ^{K×H}; log(softmax(CW^T))\\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"formulas": 0, "inline_math_repairs": 2},
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 0,
                        "formula_source_markdown_ref_count": 0,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["inline_math_residuals"], [])
        self.assertEqual(result["inline_math_repair_count"], 2)

    def test_validate_final_formula_surfaces_ignores_flattened_residuals_in_code_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text("", encoding="utf-8")
            (output_dir / "document.md").write_text(
                "```\nC ∈ R H\n```\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"formulas": 0, "inline_math_repairs": 1},
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 0,
                        "formula_source_markdown_ref_count": 0,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["inline_math_residuals"], [])

    def test_cjk_primary_formula_normalization_can_not_be_clean_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                "<html><body>中文正文保持不变。</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("中文正文保持不变。\n", encoding="utf-8")
            (output_dir / "document.json").write_text(
                adapter.json.dumps({"texts": [{"label": "formula", "text": r"x = y"}]}),
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": True,
                        "applied": True,
                        "counts": {"formulas": 1},
                        "mode": "preserve_existing_cjk_body_source_visual_authoritative",
                        "authoritative_surfaces": ["document.html", "document.md"],
                        "reason": "cjk_machine_formula_normalization_unavailable:Test",
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 1,
                        "formula_source_markdown_ref_count": 1,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["machine_surface_ok"])
        self.assertEqual(result["formula_count"], 1)
        self.assertIn("machine_formula_surface_warning:incomplete_mathml_coverage", status["warnings"])
        self.assertEqual(status["success_class"], "degraded_success")

    def test_cjk_formula_source_blocks_without_source_anchors_use_occurrence_linking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps({"texts": [{"label": "formula", "text": "x"}, {"label": "formula", "text": "y"}]}),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<html><body>"
                '<div data-formula-index="1">公式占位 A</div>'
                '<div data-formula-index="2">公式占位 B</div>'
                "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "正文 $$x$$\n正文 $$y$$\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": False,
                        "applied": True,
                        "counts": {"formulas": 2},
                        "mode": "preserve_existing_cjk_body_source_visual_authoritative",
                        "authoritative_surfaces": ["document.html", "document.md"],
                        "reason": "cjk_machine_formula_normalization_unavailable:synthetic",
                    },
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [1, 2],
                        "formula_source_html_indexes": [1, 2],
                        "formula_source_markdown_indexes": [1, 2],
                        "formula_source_missing": [],
                        "formula_source_unexpected": [],
                        "formula_source_dropped": [],
                        "formula_source_html_ref_count": 2,
                        "formula_source_markdown_ref_count": 2,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["machine_surface_ok"])
        self.assertEqual(result["formula_count"], 2)
        self.assertEqual(status["success_class"], "degraded_success")

    def test_inline_math_source_regions_require_exact_anchor_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    "<p>first <!-- source-inline-math-anchor:inline-math-cjk-1 --></p>"
                    "<p>second <!-- source-inline-math-anchor:inline-math-cjk-2 --></p>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "inline 1 <!-- source-inline-math-anchor:inline-math-cjk-1 -->\n\n"
                "inline 2 <!-- source-inline-math-anchor:inline-math-cjk-2 -->\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"formulas": 0, "inline_math_repairs": 0},
                        "inline_math_source_regions": [
                            {
                                "anchor": "inline-math-cjk-1",
                                "page_no": 1,
                                "source_image": "formulas/inline_math_1.png",
                            },
                            {
                                "anchor": "inline-math-cjk-2",
                                "page_no": 1,
                                "source_image": "formulas/inline_math_2.png",
                            },
                        ],
                    },
                    "final_source_visuals": {
                        "inline_math_source_expected_anchors": [
                            "inline-math-cjk-1",
                            "inline-math-cjk-2",
                        ],
                        "inline_math_source_html_anchors": [
                            "inline-math-cjk-1",
                            "inline-math-cjk-2",
                        ],
                        "inline_math_source_markdown_anchors": [
                            "inline-math-cjk-1",
                            "inline-math-cjk-2",
                        ],
                        "formula_source_expected_indexes": [],
                        "formula_source_html_indexes": [],
                        "formula_source_markdown_indexes": [],
                    },
                },
            }

            (output_dir / "formulas").mkdir()
            (output_dir / "formulas" / "inline_math_1.png").write_bytes(b"png")
            (output_dir / "formulas" / "inline_math_2.png").write_bytes(b"png")

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])

    def test_validate_final_formula_surfaces_rejects_exact_formula_source_index_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps({"texts": [{"label": "formula", "text": "x"}, {"label": "formula", "text": "y"}]}),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                '<div class="formula"><math><mi>x</mi></math></div>'
                '<div class="formula"><math><mi>y</mi></math></div>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x$$\n$$y$$\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"formulas": 2},
                        "authoritative_surfaces": ["document.html", "document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [1, 2],
                        "formula_source_html_indexes": [10, 11],
                        "formula_source_markdown_indexes": [10, 11],
                        "formula_source_missing": [],
                        "formula_source_unexpected": [],
                        "formula_source_dropped": [],
                        "formula_source_html_ref_count": 2,
                        "formula_source_markdown_ref_count": 2,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir,
                {},
                status,
            )
            self.assertFalse(result["ok"])
            self.assertIn(
                "incomplete_formula_source_visual_coverage", result["failure_reasons"]
            )

    def test_formula_source_exact_index_gaps_fail_even_when_mathml_markdown_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {
                        "texts": [
                            {"label": "formula", "text": "x"},
                            {"label": "formula", "text": "y"},
                            {"label": "formula", "text": "z"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>"
                "<div><math><mi>y</mi></math></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x$$\n$$y$$\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": True,
                        "applied": True,
                        "counts": {"formulas": 2},
                        "authoritative_surfaces": ["document.html", "document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [1, 2, 3],
                        "formula_source_html_indexes": [1, 2],
                        "formula_source_markdown_indexes": [1, 2],
                        "formula_source_missing": [3],
                        "formula_source_unexpected": [],
                        "formula_source_dropped": [],
                        "formula_source_html_ref_count": 3,
                        "formula_source_markdown_ref_count": 3,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir,
                {},
                status,
            )

            self.assertFalse(result["ok"])
            self.assertIn(
                "incomplete_formula_source_visual_coverage",
                result["failure_reasons"],
            )
            self.assertFalse(status["ok"])

    def test_formula_source_drops_allow_compact_fragment_missing_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {
                        "texts": [
                            {"label": "formula", "text": "x"},
                            {"label": "formula", "text": "y"},
                            {"label": "formula", "text": "z"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>"
                "<div><math><mi>y</mi></math></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x$$\n$$y$$\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": True,
                        "applied": True,
                        "counts": {"formulas": 2},
                        "authoritative_surfaces": ["document.html", "document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [1, 2, 3],
                        "formula_source_html_indexes": [1, 2],
                        "formula_source_markdown_indexes": [1, 2],
                        "formula_source_missing": [3],
                        "formula_source_unexpected": [],
                        "formula_source_dropped": [{"index": 3, "reason": "compact_formula_fragment"}],
                        "formula_source_html_appendix_indexes": [3],
                        "formula_source_markdown_appendix_indexes": [3],
                        "formula_source_html_ref_count": 3,
                        "formula_source_markdown_ref_count": 3,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir,
                {},
                status,
            )

            self.assertTrue(result["ok"])
            self.assertNotIn(
                "incomplete_formula_source_visual_coverage",
                result["failure_reasons"],
            )

    def test_formula_source_unknown_missing_raw_index_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {
                        "texts": [
                            {"label": "formula", "text": "x"},
                            {"label": "formula", "text": "y"},
                            {"label": "formula", "text": "z"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>"
                "<div><math><mi>y</mi></math></div>"
                "<div><math><mi>z</mi></math></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x$$\n$$y$$\n$$z$$\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"formulas": 3},
                        "authoritative_surfaces": ["document.html", "document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [1, 2, 3],
                        "formula_source_html_indexes": [1, 2],
                        "formula_source_markdown_indexes": [1, 2],
                        "formula_source_missing": [3],
                        "formula_source_unexpected": [],
                        "formula_source_dropped": [{"index": 3, "reason": "user_configured_skip"}],
                        "formula_source_html_ref_count": 3,
                        "formula_source_markdown_ref_count": 3,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir,
                {},
                status,
            )

            self.assertFalse(result["ok"])
            self.assertIn(
                "incomplete_formula_source_visual_coverage",
                result["failure_reasons"],
            )

    def test_primary_formula_failure_remains_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {"texts": [{"label": "formula", "text": r"x = y \quad (1)"}]},
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<div><math><mi>x</mi></math></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x = y \\quad (1)$$\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": False,
                "success_class": "degraded_failure",
                "warnings": ["upstream_failure"],
                "quality_signals": {
                    "primary_surface": {
                        "ok": False,
                        "counts": {"formulas": 1},
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 1,
                        "formula_source_markdown_ref_count": 1,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertFalse(status["ok"])
        self.assertIn("upstream_failure", status["warnings"])
        self.assertTrue(result["ok"])

    def test_formula_raw_html_tokens_and_placeholders_fail_without_source_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {"texts": [{"label": "formula", "text": r"x = y \quad (1)"}]},
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                r"<div><formula>x = y \\quad (1)</formula></div>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x = y \\quad (1)$$<!-- formula-not-decoded -->\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "ok": False,
                        "counts": {"formulas": 1},
                        "authoritative_surfaces": ["document.md"],
                    },
                    "final_source_visuals": {
                        "formula_source_html_ref_count": 1,
                        "formula_source_markdown_ref_count": 1,
                    },
                },
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertFalse(result["ok"])
        self.assertIn("raw_formula_model_tokens", result["failure_reasons"])
        self.assertIn("undecoded_formula_placeholders", result["failure_reasons"])

    def test_document_formula_count_and_placeholders_survive_failed_reflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.json").write_text(
                json.dumps({"texts": [{"label": "formula", "text": "x=y"}]}),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<html><body></body></html>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text(
                "<!-- formula-not-decoded -->\n", encoding="utf-8"
            )
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": False,
                "success_class": "degraded_failure",
                "warnings": [],
                "quality_signals": {"primary_surface": {"ok": False}},
            }

            result = adapter.validate_final_formula_surfaces(
                output_dir, metadata, status
            )

        self.assertEqual(result["formula_count"], 1)
        self.assertEqual(result["undecoded_placeholder_count"], 1)
        self.assertIn("undecoded_formula_placeholders", result["failure_reasons"])

    def test_empty_table_visual_fallback_replaces_semantic_empty_table_figure(self) -> None:
        document = {
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "caption",
                    "text": "Table 1: Empty table with semantic caption.",
                    "prov": [{"page_no": 1}],
                }
            ],
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "label": "table",
                    "captions": [{"$ref": "#/texts/0"}],
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "tables").mkdir()
            (output_dir / "tables" / "table_1.png").write_bytes(b"png")
            (output_dir / "document.html").write_text(
                '<figure class="semantic-table"><table><caption>Table 1: Empty table with semantic caption.</caption></table></figure>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "Table 1: Empty table with semantic caption.\n",
                encoding="utf-8",
            )

            result = adapter.inject_empty_table_visual_fallbacks(
                output_dir,
                document,
                document["tables"],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["html_applied_count"], 1)
        self.assertEqual(result["markdown_applied_count"], 1)
        self.assertNotIn("<table", html_text)
        self.assertIn("docling-table-visual-fallback", html_text)
        self.assertIn("![Table 1: Empty table with semantic caption.](tables/table_1.png)", markdown)

    def test_validate_final_structural_surfaces_rejects_unverified_algorithm_step_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    '<section class="docling-algorithm">'
                    "\n1: input x\n3: output y\n"
                    "</section>"
                    "<table><tr><td>1</td></tr></table>"
                    "<section class=\"code-listing\"><pre><code>print(1)</code></pre></section>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "![Algorithm](algorithms/algorithm_1.png)\n\n" "![Table](tables/table_1.png)\n\n" "```\ncode\n```\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {
                            "tables": 1,
                            "algorithms": 1,
                            "code_blocks": 1,
                        }
                    },
                    "final_source_visuals": {
                        "table_source_html_ref_count": 1,
                        "table_source_markdown_ref_count": 1,
                        "algorithm_source_html_ref_count": 1,
                        "algorithm_source_markdown_ref_count": 1,
                        "code_source_html_ref_count": 1,
                        "code_source_markdown_ref_count": 1,
                    },
                },
            }

            result = adapter.validate_final_structural_surfaces(
                output_dir,
                {},
                status,
            )

        self.assertFalse(result["ok"])
        self.assertIn("algorithm_step_identity_mismatch", result["failure_reasons"])
        self.assertEqual(result["warning_reasons"], ["machine_algorithm_step_discontinuity_source_visual_authoritative"])

    def test_validate_final_structural_surfaces_fails_on_empty_semantic_table_and_missing_source_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    '<figure class="semantic-table"><table></table></figure>'
                    '<section class="docling-algorithm"><p>1: first</p></section>'
                    '<section class="code-listing"><pre><code>print()</code></pre></section>'
                    "<table><tr><td>a</td></tr></table>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "table text\n",
                encoding="utf-8",
            )

            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {
                            "tables": 1,
                            "algorithms": 1,
                            "code_blocks": 1,
                        }
                    },
                    "final_source_visuals": {
                        "table_source_html_ref_count": 0,
                        "table_source_markdown_ref_count": 0,
                        "algorithm_source_html_ref_count": 0,
                        "algorithm_source_markdown_ref_count": 0,
                        "code_source_html_ref_count": 0,
                        "code_source_markdown_ref_count": 0,
                    },
                },
            }

            result = adapter.validate_final_structural_surfaces(
                output_dir,
                {},
                status,
            )

        self.assertFalse(result["ok"])
        self.assertIn("empty_semantic_table_without_visual_replacement", result["failure_reasons"])
        self.assertIn("incomplete_table_source_visual_coverage", result["failure_reasons"])
        self.assertIn("incomplete_algorithm_source_visual_coverage", result["failure_reasons"])
        self.assertIn("incomplete_code_source_visual_coverage", result["failure_reasons"])
        self.assertFalse(status["ok"])
        self.assertEqual(status["success_class"], "degraded_failure")

    def test_count_only_footnote_match_does_not_clear_stale_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                '<a id="fnref-star-1" href="#fn-star-1">∗</a>'
                '<aside id="fn-star-1">⋆ Corresponding author</aside>',
                encoding="utf-8",
            )
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "warnings": [
                    "suspicious_footnote:1:near_page_bottom_footnote",
                    "unresolved_note_reference:page=1:marker=*",
                    "structural_quarantine_applied:candidates=1",
                    "formula_second_pass_skipped:no_formula_candidates",
                ],
                "quality_signals": {
                    "primary_surface": {"ok": True},
                    "structural_quarantine_qc": {
                        "unresolved_footnote_count": 1,
                    },
                },
            }

            result = adapter.reconcile_final_surface_status(
                output_dir, metadata, status
            )

        self.assertFalse(result["pre_reflow_diagnostics_resolved"])
        self.assertEqual(result["linked_footnote_count"], 1)
        self.assertEqual(result["removed_stale_warning_count"], 0)
        self.assertEqual(
            status["warnings"],
            [
                "suspicious_footnote:1:near_page_bottom_footnote",
                "unresolved_note_reference:page=1:marker=*",
                "structural_quarantine_applied:candidates=1",
                "formula_second_pass_skipped:no_formula_candidates",
            ],
        )
        self.assertEqual(metadata["final_surface_reconciliation"], result)


class PortableFormulaOcrTests(unittest.TestCase):
    def test_formula_crop_tightening_removes_detached_prose_lines(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ModuleNotFoundError:
            self.skipTest("Pillow unavailable")
        image = Image.new("RGB", (300, 100), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((5, 8, 294, 19), fill="black")
        draw.rectangle((5, 34, 294, 45), fill="black")
        draw.rectangle((125, 76, 255, 90), fill="black")
        draw.rectangle((280, 76, 294, 90), fill="black")
        payload = io.BytesIO()
        image.save(payload, format="PNG")

        tightened, diagnostic = adapter._tighten_formula_ocr_crop(
            payload.getvalue()
        )
        tightened_image = Image.open(io.BytesIO(tightened))

        self.assertTrue(diagnostic["applied"])
        self.assertLess(tightened_image.height, image.height // 2)
        self.assertLess(tightened_image.width, image.width)

    def test_formula_service_url_is_restricted_to_private_sidecar(self) -> None:
        self.assertEqual(
            adapter.validated_private_formula_ocr_url("http://formula:8001/"),
            "http://formula:8001",
        )
        with self.assertRaisesRegex(ValueError, "local Docker/loopback"):
            adapter.validated_private_formula_ocr_url("https://example.com/formulas")

    def test_private_formula_service_patches_placeholder_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            (output_dir / "formulas" / "formula_1.png").write_bytes(b"png")
            (output_dir / "document.json").write_text(
                json.dumps(
                    {
                        "texts": [
                            {
                                "label": "formula",
                                "text": "",
                                "prov": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                "<html><head></head><body><p>Formula follows.</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "Formula follows.\n\n<!-- formula-not-decoded -->\n",
                encoding="utf-8",
            )
            metadata: dict[str, object] = {
                "ocr_fallback_reason": "gxx_quality_failure"
            }
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            args = Namespace(
                formula_policy="formula_service",
                enable_formula_mlx=False,
                formula_ocr_url="http://formula:8001",
                input_file=output_dir / "source.pdf",
                timeout_seconds=60,
                http_retries=0,
                http_retry_sleep_seconds=0,
            )
            response = {
                "ok": True,
                "model": "test/guarded-ensemble",
                "results": [
                    {
                        "id": "1",
                        "latex": "x = y",
                        "ok": True,
                        "safety_reasons": [],
                        "variant": "source_crop",
                    }
                ],
            }
            with patch.object(adapter, "post_json", return_value=response) as post:
                result = adapter.run_portable_formula_ocr(
                    output_dir, metadata, status, args
                )

            document = json.loads((output_dir / "document.json").read_text())
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")
            request_payload = post.call_args.args[1]

        self.assertTrue(result["ok"])
        self.assertEqual(result["patched_indexes"], [1])
        self.assertEqual(document["texts"][0]["text"], "x = y")
        self.assertEqual(result["document_md_patched"], [1])
        self.assertIn("$$\nx = y\n$$", markdown)
        self.assertNotIn("formula-not-decoded", markdown)
        self.assertFalse(result["source_semantic_gate"]["enabled"])
        self.assertIsNone(request_payload["items"][0]["source_text"])
        self.assertTrue(status["ok"])


class SemanticReflowTests(unittest.TestCase):
    def test_structure_block_source_refs_follow_nodes_when_blocks_reorder(self) -> None:
        class FakeSource:
            def lines(self, _prov, padding=0.0):
                del padding
                return []

            def text(self, _prov):
                return ""

            def page_size(self, _page_no):
                return 612.0, 792.0

        source = FakeSource()
        algorithm = semantic_reflow.FlowItem(
            kind="algorithm",
            node={
                "self_ref": "#/tables/8",
                "label": "code",
                "text": "Algorithm 1 Input: x",
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 1.0, "r": 80.0, "t": 80.0, "b": 40.0},
            prov={"page_no": 1, "bbox": {"l": 1.0, "r": 80.0, "t": 80.0, "b": 40.0}},
            collection_index=8,
        )
        code = semantic_reflow.FlowItem(
            kind="code",
            node={"self_ref": "#/texts/12", "label": "code", "text": "print(1)"},
            rank=2.0,
            page_no=1,
            bbox={"l": 1.0, "r": 80.0, "t": 35.0, "b": 10.0},
            prov={"page_no": 1, "bbox": {"l": 1.0, "r": 80.0, "t": 35.0, "b": 10.0}},
            collection_index=12,
        )
        html_before, md_before, _ = semantic_reflow._render(
            [algorithm, code], {"name": "Identity"}, source
        )
        html_after, md_after, _ = semantic_reflow._render(
            [code, algorithm], {"name": "Identity"}, source
        )

        for rendered_html, rendered_md in (
            (html_before, md_before),
            (html_after, md_after),
        ):
            self.assertIn('data-source-ref="#/tables/8"', rendered_html)
            self.assertIn('data-source-ref="#/texts/12"', rendered_html)
            self.assertIn("<!-- source-ref:#/tables/8 -->", rendered_md)
            self.assertIn("<!-- source-ref:#/texts/12 -->", rendered_md)
        self.assertEqual(
            {
                "#/tables/8",
                "#/texts/12",
            },
            {
                semantic_reflow._structure_block_source_ref(item)
                for item in (algorithm, code)
            },
        )

    def test_structure_block_source_ref_has_stable_kind_index_fallback(self) -> None:
        class FakeSource:
            def lines(self, _prov, padding=0.0):
                del padding
                return []

            def text(self, _prov):
                return ""

            def page_size(self, _page_no):
                return 612.0, 792.0

        source = FakeSource()
        item = semantic_reflow.FlowItem(
            kind="table",
            node={
                "label": "table",
                "data": {
                    "num_rows": 1,
                    "num_cols": 1,
                    "table_cells": [{
                        "start_row_offset_idx": 0,
                        "start_col_offset_idx": 0,
                        "text": "value",
                    }],
                },
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 1.0, "r": 80.0, "t": 80.0, "b": 40.0},
            prov={"page_no": 1, "bbox": {"l": 1.0, "r": 80.0, "t": 80.0, "b": 40.0}},
            collection_index=7,
        )
        html_text, markdown, _ = semantic_reflow._render(
            [item], {"name": "Identity"}, source
        )

        self.assertEqual(
            semantic_reflow._structure_block_source_ref(item),
            "table:7",
        )
        self.assertIn('data-source-ref="table:7"', html_text)
        self.assertIn("<!-- source-ref:table:7 -->", markdown)

    def test_repair_flattened_inline_math_does_not_guess_without_source_geometry(self) -> None:
        for value in (
            "rate n - 1/2 under",
            "R d",
            "C ∈ R H and W ∈ R K × H; log(softmax(CW T))",
            "X i",
            "H K i",
            "H K P 0",
            "X 2 i",
            "T P 0",
            "R(d=1)",
        ):
            self.assertEqual(semantic_reflow._repair_flattened_inline_math(value), value)

    def test_repair_flattened_inline_math_keeps_combining_slash_operators_stable(self) -> None:
        self.assertEqual(
            semantic_reflow._repair_source_comparison_operators(
                "phi(x) \u0338= 0 and z \u0338\u2208 x",
                "phi(x) \u0338= 0 and z \u0338\u2208 x",
            ),
            "phi(x) ≠ 0 and z ∉ x",
        )

    def test_source_comparison_overlay_restores_semantic_negation(self) -> None:
        self.assertEqual(
            semantic_reflow._repair_source_comparison_operators(
                "P(n)=P0 and phi(x)=0",
                "P(n)\u0338=P0 and phi(x)\u0338=0",
            ),
            "P(n)≠P0 and phi(x)≠0",
        )
        self.assertEqual(
            semantic_reflow._repair_source_comparison_operators(
                "R(d=1)",
                "R(d=1)",
            ),
            "R(d=1)",
        )
        self.assertEqual(
            semantic_reflow._repair_source_comparison_operators(
                "phi(x) \u0338= 0",
                "phi(x) \u0338= 0",
            ),
            "phi(x) ≠ 0",
        )

    def test_source_comparison_does_not_zip_unrelated_inequalities(self) -> None:
        # Real 2607.24235 p4/p12 paragraphs contain ordinary >, ≥ and =
        # operators next to one another.  Only a combining strike may transfer
        # semantic negation; extraction-order differences must be preserved.
        self.assertEqual(
            semantic_reflow._repair_source_comparison_operators(
                "C > 0 and n > n0",
                "C = 0 and n ≥ n0",
            ),
            "C > 0 and n > n0",
        )
        self.assertEqual(
            semantic_reflow._repair_source_comparison_operators(
                "n ≥ n0 and p = 0",
                "n > n0 and p = 0",
            ),
            "n ≥ n0 and p = 0",
        )

    def test_cjk_inline_math_hint_detects_common_mixed_and_private_patterns(self) -> None:
        for value in (
            "在 R d 中定义",
            "该公式中 X i 表示",
            "我们令 ER′x 为目标函数",
            "该文提到 C ∈ R H",
            "存在 (cid:7) 的私有字形标记",
        ):
            self.assertTrue(semantic_reflow._cjk_inline_math_hint(value))

        self.assertFalse(semantic_reflow._cjk_inline_math_hint("这是一句纯中文正文"))
        self.assertFalse(
            semantic_reflow._cjk_inline_math_hint(
                "计算机应用研究 Application Research of Computers"
            )
        )
        self.assertFalse(
            semantic_reflow._cjk_inline_math_hint("中图分类号：TP183")
        )
        for model_prose in (
            "中文模型 TCN-KT/CKT/MAFKT 在 batch-size 32 下训练。",
            "中文模型 GKT 10/GIKTD 在数据集上比较。",
        ):
            self.assertFalse(semantic_reflow._cjk_inline_math_hint(model_prose))

    def test_collect_cjk_inline_math_source_regions_supports_chunked_documents_and_global_indexes(self) -> None:
        class FakeSourceReader:
            def _pypdfium_characters(self, page_no, _bbox):
                return [
                    {"text": "R", "bbox": {"l": 1.0, "r": 2.0, "t": 8.0, "b": 0.0}, "fontname": "CMMI10"},
                    {"text": "d", "bbox": {"l": 2.1, "r": 3.5, "t": 8.2, "b": 0.0}, "fontname": "CMMI10"},
                ]

            def text(self, _prov):
                return "R d"

        document = {
            "chunks": [
                {
                    "page_range": [3, 3],
                    "document": {
                        "texts": [
                            {
                                "label": "text",
                                "text": "这段文字包含ER d 的说明",
                                "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                            }
                        ]
                    },
                },
                {
                    "page_range": [1, 1],
                    "document": {
                        "texts": [
                            {
                                "label": "text",
                                "text": "在定义中，x i 表示样本",
                                "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                            },
                            {
                                "label": "text",
                                "text": "实验中 C ∈ R H",
                                "prov": [{"page_no": 2, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                            },
                        ]
                    },
                },
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    "<p>在定义中，x i 表示样本</p>"
                    "<p>实验中 C ∈ R H</p>"
                    "<p>这段文字包含ER d 的说明</p>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    "在定义中，x i 表示样本\n"
                    "实验中 C ∈ R H\n"
                    "这段文字包含ER d 的说明\n"
                ),
                encoding="utf-8",
            )

            result = semantic_reflow._collect_cjk_inline_math_source_regions(
                output_dir,
                document,
                FakeSourceReader(),
            )

        regions = result["regions"]
        self.assertEqual(len(result["missing"]), 0)
        self.assertEqual(result["html_anchor_count"], 3)
        self.assertEqual(result["markdown_anchor_count"], 3)
        self.assertEqual(len(regions), 3)
        self.assertEqual({region["part_index"] for region in regions}, {0, 1})
        collection_indexes = [region["collection_index"] for region in regions]
        self.assertEqual(collection_indexes, [0, 1, 2])
        self.assertTrue(all(region["anchor"].startswith("inline-math-chunk") for region in regions))
        self.assertEqual(
            [int(region["anchor"].split("-")[2].removeprefix("chunk")) for region in regions],
            [0, 0, 1],
        )
        self.assertIn("inline-math-chunk0", regions[0]["anchor"])
        self.assertIn("inline-math-chunk1", regions[2]["anchor"])

    def test_collect_cjk_inline_math_source_regions_binds_html_escaped_characters(self) -> None:
        class FakeSourceReader:
            def _pypdfium_characters(self, _page_no, _bbox):
                return [
                    {
                        "text": "C",
                        "bbox": {"l": 1.0, "r": 2.0, "t": 12.0, "b": 0.0},
                        "fontname": "CMMI10",
                    },
                    {
                        "text": "<",
                        "bbox": {"l": 2.1, "r": 3.0, "t": 12.0, "b": 0.0},
                        "fontname": "CMMI10",
                    },
                    {
                        "text": "&",
                        "bbox": {"l": 3.2, "r": 4.0, "t": 12.0, "b": 0.0},
                        "fontname": "CMMI10",
                    },
                ]

            def text(self, _prov):
                return "C<\u0026"

        node_text = "在 C ∈ R H 和 N<0、A&B 的例子"
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{html.escape(node_text)}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"{node_text}\n",
                encoding="utf-8",
            )
            result = semantic_reflow._collect_cjk_inline_math_source_regions(
                output_dir,
                {
                    "texts": [
                        {
                            "label": "text",
                            "text": node_text,
                            "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                        }
                    ]
                },
                FakeSourceReader(),
            )

        self.assertEqual(result["missing"], [])
        self.assertEqual(result["html_anchor_count"], 1)
        self.assertEqual(result["markdown_anchor_count"], 1)
        self.assertEqual(len(result["regions"]), 1)
        self.assertIn("inline-math-chunk0-", result["regions"][0]["anchor"])

    def test_collect_cjk_inline_math_source_regions_uses_appendix_for_nonunique_binding(self) -> None:
        class FakeSourceReader:
            def _pypdfium_characters(self, _page_no, _bbox):
                return [
                    {"text": "R", "bbox": {"l": 1.0, "r": 2.0, "t": 8.0, "b": 0.0}, "fontname": "CMSY10"},
                ]

            def text(self, _prov):
                return "R d"

        source = FakeSourceReader()
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "重复文本 R d 用于重复匹配",
                    "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                },
                {
                    "label": "text",
                    "text": "重复文本 R d 用于重复匹配",
                    "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                },
                {
                    "label": "text",
                    "text": "缺失证据 R d",
                    "prov": [{"page_no": 0, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                },
                {
                    "label": "text",
                    "text": "可收集的 ERx 情况",
                    "prov": [],
                },
                {
                    "label": "text",
                    "text": "唯一 R x 可收集",
                    "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                },
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    "<p>重复文本 R d 用于重复匹配</p>"
                    "<p>重复文本 R d 用于重复匹配</p>"
                    "<p>缺失证据 R d</p>"
                    "<p>可收集的 ERx 情况</p>"
                    "<p>唯一 R x 可收集</p>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    "重复文本 R d 用于重复匹配\n\n"
                    "重复文本 R d 用于重复匹配\n\n"
                    "缺失证据 R d\n"
                    "可收集的 ERx 情况\n"
                    "唯一 R x 可收集\n"
                ),
                encoding="utf-8",
            )

            result = semantic_reflow._collect_cjk_inline_math_source_regions(
                output_dir,
                document,
                source,
            )
            # A release retry must replace prior markers rather than stack
            # duplicate occurrence anchors.
            result_retry = semantic_reflow._collect_cjk_inline_math_source_regions(
                output_dir,
                document,
                source,
            )
            html_after = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_after = (output_dir / "document.md").read_text(encoding="utf-8")

        regions = result["regions"]
        missing = result["missing"]
        reasons = {item["reason"] for item in missing}
        self.assertIn("cjk_inline_body_node_bbox_unavailable", reasons)
        self.assertIn("cjk_inline_body_node_provenance_not_unique", reasons)
        self.assertNotIn("cjk_inline_body_node_not_uniquely_bindable", reasons)
        self.assertEqual(len(regions), 3)
        self.assertEqual(
            {region["binding_mode"] for region in regions},
            {"appendix", "inline"},
        )
        self.assertEqual(
            len(
                [
                    region
                    for region in regions
                    if region["binding_mode"] == "appendix"
                ]
            ),
            2,
        )
        self.assertEqual(regions[-1]["collection_index"], 4)
        self.assertTrue(regions[-1]["anchor"].startswith("inline-math-chunk0-texts-4-0-"))
        self.assertEqual(result["html_anchor_count"], 1)
        self.assertEqual(result["markdown_anchor_count"], 1)
        self.assertEqual(result["appendix_anchor_count"], 2)
        self.assertEqual(len(result["binding_diagnostics"]), 2)
        self.assertEqual(result_retry["appendix_anchor_count"], 2)
        self.assertIn("Inline math source review appendix", html_after)
        self.assertIn("Inline math source review appendix", markdown_after)
        self.assertLess(
            html_after.index("docling-inline-math-source-appendix"),
            html_after.lower().index("</body>"),
        )
        self.assertEqual(html_after.count("source-inline-math-anchor:"), 3)
        self.assertEqual(markdown_after.count("source-inline-math-anchor:"), 3)

    def test_source_math_evidence_supports_math_font_private_glyph_and_script_signal(self) -> None:
        self.assertTrue(
            semantic_reflow._cjk_inline_math_source_evidence(
                "混排的 ER d", [{"text": "(cid:23)", "bbox": {"l": 1, "r": 2, "t": 2, "b": 0}, "size": 10}]
            )
        )
        self.assertTrue(
            semantic_reflow._cjk_inline_math_source_evidence(
                "混排的 R d",
                [
                    {"text": "R", "bbox": {"l": 1, "r": 2, "t": 12, "b": 0}, "size": 10},
                    {"text": "d", "bbox": {"l": 2.1, "r": 3, "t": 8, "b": 0}, "size": 6},
                ],
            )
        )

    def test_geometry_repair_requires_local_script_evidence(self) -> None:
        runs = [
            {"text": "N", "size": 10.9, "fontname": "MSBM10", "bbox": {"l": 10, "r": 17, "t": 12, "b": 0}},
            {"text": "0", "size": 8.0, "fontname": "CMR8", "bbox": {"l": 17.5, "r": 21, "t": 5, "b": 0}},
            {"text": "X", "size": 10.9, "fontname": "CMI10", "bbox": {"l": 40, "r": 47, "t": 12, "b": 0}},
            # A small glyph on the same baseline is not evidence of a
            # subscript; this negative case guards against size-only repairs.
            {"text": "i", "size": 8.0, "fontname": "CMR8", "bbox": {"l": 47.5, "r": 50, "t": 9, "b": 3}},
        ]
        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "N 0; The X i variable remains prose.",
            runs,
            math_font_evidence=lambda run: str(run.get("fontname") or "").startswith(("MSBM", "CMI")),
        )
        # Blackboard conversion is allowed only with explicit MSBM evidence;
        # ordinary CMR/CMI runs must keep their extracted base glyphs.
        self.assertIn("ℕ_0", repaired)
        self.assertIn("geometry_script-N-sub-0-run0", names)
        self.assertNotIn("X_i", repaired)
        self.assertEqual(unresolved, set())

    def test_geometry_repair_restores_missing_blackboard_base_from_unique_script(self) -> None:
        runs = [
            {"text": "R", "size": 10.9, "fontname": "MSBM10", "bbox": {"l": 0, "r": 7, "t": 12, "b": 0}},
            {"text": "B", "size": 8.0, "fontname": "CMMI8", "bbox": {"l": 7.4, "r": 11, "t": 16, "b": 8}},
            {"text": "×", "size": 8.0, "fontname": "CMSY8", "bbox": {"l": 11.4, "r": 15, "t": 16, "b": 8}},
            {"text": "L", "size": 8.0, "fontname": "CMMI8", "bbox": {"l": 15.4, "r": 19, "t": 16, "b": 8}},
            {"text": "×", "size": 8.0, "fontname": "CMSY8", "bbox": {"l": 19.4, "r": 23, "t": 16, "b": 8}},
            {"text": "D", "size": 8.0, "fontname": "CMMI8", "bbox": {"l": 23.4, "r": 27, "t": 16, "b": 8}},
        ]

        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "B × L × D",
            runs,
            math_font_evidence=lambda run: "MSBM" in str(run.get("fontname") or ""),
        )

        self.assertEqual("ℝ^{B×L×D}", repaired)
        self.assertEqual(1, len(names))
        self.assertEqual(set(), unresolved)

    def test_geometry_repair_allows_proven_roman_algorithm_subscript(self) -> None:
        runs = [
            {"text": "x", "size": 10.9, "fontname": "LMROMAN10-BOLD", "bbox": {"l": 10, "r": 16, "t": 12, "b": 0}},
            {"text": "o", "size": 8.0, "fontname": "LMROMAN8", "bbox": {"l": 16.3, "r": 19, "t": 7, "b": -1}},
            {"text": "u", "size": 8.0, "fontname": "LMROMAN8", "bbox": {"l": 19.2, "r": 22, "t": 7, "b": -1}},
            {"text": "t", "size": 8.0, "fontname": "LMROMAN8", "bbox": {"l": 22.2, "r": 25, "t": 7, "b": -1}},
            {"text": "∈", "size": 10.9, "fontname": "CMSY10", "bbox": {"l": 28, "r": 34, "t": 12, "b": 0}},
        ]

        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "Ensure x ∈",
            runs,
            math_font_evidence=lambda _run: False,
            allow_text_script_base=True,
        )

        self.assertEqual("Ensure x_{out} ∈", repaired)
        self.assertEqual(1, len(names))
        self.assertEqual(set(), unresolved)

    def test_geometry_repair_keeps_non_msmb_blackboard_base_as_plain_text(self) -> None:
        runs = [
            {
                "text": "N",
                "size": 10.9,
                "fontname": "CMR10",
                "bbox": {"l": 10, "r": 17, "t": 12, "b": 0},
            },
            {
                "text": "0",
                "size": 8.0,
                "fontname": "CMR8",
                "bbox": {"l": 17.5, "r": 21, "t": 5, "b": 0},
            },
        ]
        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "N 0",
            runs,
            math_font_evidence=lambda run: str(run.get("fontname") or "").startswith("CMR"),
        )
        self.assertEqual(repaired, "N_0")
        self.assertIn("geometry_script-N-sub-0-run0", names)
        self.assertEqual(unresolved, set())

    def test_geometry_repair_recovers_nested_multichar_scripts_locally(self) -> None:
        runs = [
            {
                "text": "H",
                "size": 10.0,
                "fontname": "CMMI10",
                "bbox": {"l": 10, "r": 16, "t": 12, "b": 0},
            },
            {
                "text": "K",
                "size": 7.0,
                "fontname": "CMMI7",
                "bbox": {"l": 16.5, "r": 20, "t": 7, "b": 1},
            },
            {
                "text": "i",
                "size": 5.5,
                "fontname": "CMMI5",
                "bbox": {"l": 20.2, "r": 22, "t": 5, "b": 0},
            },
        ]
        diagnostics: list[dict[str, object]] = []
        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "H K i",
            runs,
            math_font_evidence=lambda run: "CMMI" in str(run.get("fontname") or ""),
            cluster_diagnostics=diagnostics,
        )
        self.assertEqual(repaired, "H_{K_i}")
        self.assertEqual(len(names), 1)
        self.assertEqual(unresolved, set())
        self.assertEqual(len(diagnostics), 2)
        resolved = [item for item in diagnostics if item.get("resolved")]
        suppressed = [item for item in diagnostics if item.get("suppressed")]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(resolved[0]["source_text"], "HKi")

    def test_geometry_repair_groups_opposite_role_product_limits_on_one_base(self) -> None:
        runs = [
            {"text": "×", "size": 10.9, "fontname": "CMSY10", "bbox": {"l": 0, "r": 5.3, "t": 6.5, "b": 0}},
            {"text": "d", "size": 8.0, "fontname": "CMMI8", "bbox": {"l": 6.05, "r": 10, "t": 10.6, "b": 4.9}},
            {"text": "i", "size": 8.0, "fontname": "CMMI8", "bbox": {"l": 5.95, "r": 8.2, "t": 3.3, "b": -2}},
            {"text": "=", "size": 8.0, "fontname": "CMR8", "bbox": {"l": 9.0, "r": 14.6, "t": 1.1, "b": -0.9}},
            {"text": "1", "size": 8.0, "fontname": "CMR8", "bbox": {"l": 15.9, "r": 18.7, "t": 3.3, "b": -1}},
            {"text": "X", "size": 10.9, "fontname": "CMSY10", "bbox": {"l": 20, "r": 28.3, "t": 6.5, "b": 0}},
            {"text": "i", "size": 8.0, "fontname": "CMMI8", "bbox": {"l": 27.5, "r": 29.8, "t": 4.7, "b": -1.7}},
        ]
        repaired, _names, unresolved = semantic_reflow._inline_geometry_repair(
            "× d i =1 X i",
            runs,
            math_font_evidence=lambda run: "CM" in str(run.get("fontname") or ""),
        )
        self.assertEqual(repaired, "×_{i=1}^d X_i")
        self.assertEqual(unresolved, set())

    def test_geometry_repair_emits_tight_unresolved_occurrence_for_ambiguous_alignment(self) -> None:
        runs = [
            {
                "text": "R",
                "size": 10.0,
                "fontname": "CMMI10",
                "bbox": {"l": 10, "r": 16, "t": 12, "b": 0},
            },
            {
                "text": "d",
                "size": 7.0,
                "fontname": "CMMI7",
                "bbox": {"l": 16.4, "r": 19.5, "t": 7, "b": 1},
            },
        ]
        diagnostics: list[dict[str, object]] = []
        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "R d and R d",
            runs,
            math_font_evidence=lambda run: "CMMI" in str(run.get("fontname") or ""),
            cluster_diagnostics=diagnostics,
        )
        self.assertEqual(repaired, "R d and R d")
        self.assertEqual(names, set())
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(len(diagnostics), 1)
        self.assertFalse(diagnostics[0]["resolved"])
        self.assertEqual(diagnostics[0]["bbox"], {"l": 10.0, "r": 19.5, "t": 12.0, "b": 0.0, "coord_origin": "BOTTOMLEFT"})

    def test_geometry_repair_does_not_attach_distant_punctuation_script(self) -> None:
        runs = [
            {
                "text": ",",
                "size": 10.0,
                "fontname": "CMMI10",
                "bbox": {"l": 10, "r": 12, "t": 12, "b": 0},
            },
            {
                "text": "1",
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {"l": 16.6, "r": 19, "t": 18, "b": 11},
            },
        ]
        diagnostics: list[dict[str, object]] = []
        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            ", 1",
            runs,
            math_font_evidence=lambda run: "CMMI" in str(run.get("fontname") or ""),
            cluster_diagnostics=diagnostics,
        )
        self.assertEqual(repaired, ", 1")
        self.assertEqual(names, set())
        self.assertEqual(len(unresolved), 0)
        self.assertEqual(diagnostics, [])

    def test_geometry_repair_keeps_local_em_dash_boundary_after_rate_script(self) -> None:
        runs = [
            {
                "text": "—",
                "size": 10.0,
                "fontname": "CMR10",
                "bbox": {"l": 0.0, "r": 8.0, "t": 10.0, "b": 9.8},
            },
            {
                "text": "n",
                "size": 10.0,
                "fontname": "CMMI10",
                "bbox": {"l": 8.2, "r": 13.0, "t": 12.0, "b": 0.0},
            },
            {
                "text": "−",
                "size": 7.0,
                "fontname": "CMSY7",
                "bbox": {"l": 13.2, "r": 17.8, "t": 11.5, "b": 11.1},
            },
            {
                "text": "1",
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {"l": 18.0, "r": 20.5, "t": 14.0, "b": 9.5},
            },
            {
                "text": "/",
                "size": 7.0,
                "fontname": "CMMI7",
                "bbox": {"l": 20.7, "r": 23.7, "t": 14.2, "b": 8.0},
            },
            {
                "text": "2",
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {"l": 24.0, "r": 27.0, "t": 14.0, "b": 9.5},
            },
            {
                "text": "—",
                "size": 10.0,
                "fontname": "CMR10",
                "bbox": {"l": 27.2, "r": 35.0, "t": 10.0, "b": 9.8},
            },
        ]
        repaired, _names, _unresolved = semantic_reflow._inline_geometry_repair(
            "rate n -1 / 2 -under mild",
            runs,
            math_font_evidence=lambda run: "CMMI" in str(run.get("fontname") or "")
            or "CMSY" in str(run.get("fontname") or ""),
        )
        self.assertEqual(repaired, "rate n^{-1/2} — under mild")

    def test_inline_math_span_evidence_blocks_partial_fraction_repair(self) -> None:
        class FakePage:
            height = 100.0
            lines = [
                {
                    "x0": 40.0,
                    "x1": 78.0,
                    "top": 48.0,
                    "bottom": 48.0,
                }
            ]

        class FakePdf:
            pages = [FakePage()]

        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = FakePdf()
        runs = [
            {
                "text": "P",
                "size": 8.0,
                "fontname": "CMMI8",
                "bbox": {"l": 32.0, "r": 39.0, "t": 58.0, "b": 50.0},
            },
            {
                "text": "e",
                "size": 8.0,
                "fontname": "CMMI8",
                "bbox": {"l": 45.0, "r": 49.0, "t": 56.0, "b": 50.0},
            },
            {
                "text": "S",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 49.4, "r": 53.0, "t": 60.0, "b": 54.0},
            },
            {
                "text": "j",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 53.2, "r": 56.0, "t": 47.0, "b": 41.0},
            },
        ]
        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {
                    "l": 25.0,
                    "r": 85.0,
                    "t": 70.0,
                    "b": 25.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            },
            runs,
        )
        self.assertEqual(len(spans), 1)
        self.assertIn("fraction_rule", spans[0]["reason"])
        self.assertLessEqual(spans[0]["bbox"]["l"], 32.0)
        self.assertGreaterEqual(spans[0]["bbox"]["r"], 56.0)

        diagnostics: list[dict[str, object]] = []
        repaired, names, unresolved = semantic_reflow._inline_geometry_repair(
            "P e S j",
            runs,
            math_font_evidence=lambda run: "CM" in str(run.get("fontname") or ""),
            cluster_diagnostics=diagnostics,
            blocked_bboxes=[spans[0]["bbox"]],
        )
        self.assertEqual(repaired, "P e S j")
        self.assertEqual(names, set())
        self.assertEqual(unresolved, set())
        self.assertTrue(diagnostics)
        self.assertTrue(diagnostics[0].get("suppressed"))

    def test_inline_math_span_evidence_detects_cmex_control_occurrence(self) -> None:
        class FakePage:
            height = 100.0
            lines: list[dict[str, float]] = []

        class FakePdf:
            pages = [FakePage()]

        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = FakePdf()
        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {"l": 0.0, "r": 80.0, "t": 80.0, "b": 20.0},
            },
            [
                {
                    "text": "\x10",
                    "size": 10.0,
                    "fontname": "CMEX10",
                    "bbox": {"l": 20.0, "r": 24.0, "t": 55.0, "b": 35.0},
                },
                {
                    "text": "F",
                    "size": 10.0,
                    "fontname": "CMMI10",
                    "bbox": {"l": 25.0, "r": 32.0, "t": 52.0, "b": 42.0},
                },
            ],
        )
        self.assertEqual(len(spans), 1)
        self.assertIn("cmex_control", spans[0]["reason"])
        self.assertEqual(spans[0]["bbox"]["coord_origin"], "BOTTOMLEFT")

    def test_inline_math_span_evidence_rejects_full_size_overbar(self) -> None:
        class FakePage:
            height = 100.0
            lines = [
                {
                    "x0": 40.0,
                    "x1": 48.0,
                    "top": 48.0,
                    "bottom": 48.0,
                }
            ]

        class FakePdf:
            pages = [FakePage()]

        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = FakePdf()
        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {"l": 0.0, "r": 80.0, "t": 80.0, "b": 20.0},
            },
            [
                {
                    "text": "X",
                    "size": 10.0,
                    "fontname": "CMMI10",
                    "bbox": {"l": 41.0, "r": 47.0, "t": 56.0, "b": 50.0},
                },
                {
                    "text": "S",
                    "size": 10.0,
                    "fontname": "CMMI10",
                    "bbox": {"l": 41.0, "r": 47.0, "t": 47.0, "b": 41.0},
                },
            ],
        )
        self.assertEqual(spans, [])

    def test_unknown_cid_is_preserved_as_unresolved_math_source_evidence(self) -> None:
        class FakePage:
            height = 100.0
            lines: list[dict[str, float]] = []

        class FakePdf:
            pages = [FakePage()]

        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = FakePdf()
        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {"l": 0.0, "r": 80.0, "t": 80.0, "b": 20.0},
            },
            [
                {
                    "text": "(cid:80)",
                    "size": 8.0,
                    "fontname": "CMMI8",
                    "bbox": {"l": 20.0, "r": 28.0, "t": 55.0, "b": 45.0},
                }
            ],
        )
        self.assertEqual(len(spans), 1)
        self.assertIn("(cid:80)", spans[0]["source_text"])

    def test_inline_math_top_left_bbox_normalization_and_anchor_height(self) -> None:
        self.assertEqual(
            semantic_reflow.SourceReader._normalize_bbox_for_math_order(
                {
                    "l": 10,
                    "r": 20,
                    "t": 20,
                    "b": 40,
                    "coord_origin": "TOPLEFT",
                    "page_height": 100,
                }
            ),
            (10.0, 20.0, 80.0, 60.0),
        )
        anchor = semantic_reflow._inline_math_anchor_id(
            page_no=2,
            collection="texts",
            index=4,
            offset=0,
            bbox={
                "l": 10,
                "r": 20,
                "t": 20,
                "b": 40,
                "coord_origin": "TOPLEFT",
            },
        )
        self.assertIn("-h20-tl", anchor)

    def test_extract_char_bbox_supports_dict_tuple_and_missing_data(self) -> None:
        self.assertEqual(
            semantic_reflow.SourceReader._extract_char_bbox(
                {"bbox": {"l": 1, "r": 3, "t": 4, "b": 2}},
                1,
                0,
            ),
            {"l": 1.0, "r": 3.0, "t": 4.0, "b": 2.0},
        )
        self.assertEqual(
            semantic_reflow.SourceReader._extract_char_bbox((1, 2, 3, 4), 1, 0),
            {"l": 1.0, "r": 3.0, "t": 4.0, "b": 2.0},
        )
        self.assertEqual(
            semantic_reflow.SourceReader._extract_char_bbox(None, 1, 0),
            {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0},
        )

    def test_bbox_preserves_pdf_coordinate_origin_for_source_regions(self) -> None:
        self.assertEqual(
            semantic_reflow._bbox(
                {
                    "page_no": 3,
                    "bbox": {
                        "l": 10,
                        "r": 40,
                        "t": 80,
                        "b": 60,
                        "coord_origin": "TOPLEFT",
                    },
                }
            ),
            {
                "l": 10.0,
                "r": 40.0,
                "t": 80.0,
                "b": 60.0,
                "coord_origin": "TOPLEFT",
            },
        )

    def test_rebuild_initializes_quality_status_for_direct_service_output(self) -> None:
        metadata: dict[str, object] = {}
        status: dict[str, object] = {}

        result = semantic_reflow.rebuild_semantic_surfaces(
            Path("/tmp/unused-semantic-output"),
            {},
            Path("/tmp/missing-semantic-source.pdf"),
            metadata,
            status,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(status["quality_signals"]["primary_surface"], result)
        self.assertEqual(status["warnings"], [result["reason"]])

    def test_rebuild_cjk_inline_source_without_pdf_glyphs_uses_appendix_degraded_success(self) -> None:
        class FakeSourceReader:
            def __init__(self, _path):
                return None

            def language_profile(self) -> dict[str, int]:
                return {"cjk_characters": 120, "latin_characters": 20}

            def _pypdfium_characters(self, _page_no, _bbox):
                return []

            def text(self, _prov):
                return "R d"

            def close(self):
                return None

        with patch.object(semantic_reflow, "SourceReader", FakeSourceReader):
            with tempfile.TemporaryDirectory() as tmpdir:
                output_dir = Path(tmpdir)
                (output_dir / "document.html").write_text(
                    "<html><body><p>在 R d 的示例中。</p></body></html>",
                    encoding="utf-8",
                )
                (output_dir / "document.md").write_text(
                    "在 R d 的示例中。\n", encoding="utf-8"
                )
                document = {
                    "texts": [
                        {
                            "label": "text",
                            "text": "在 R d 的示例中。",
                            "prov": [{"page_no": 1, "bbox": {"l": 1, "r": 12, "t": 72, "b": 64}}],
                        }
                    ]
                }
                metadata: dict[str, object] = {}
                status: dict[str, object] = {
                    "ok": True,
                    "success_class": "success",
                    "warnings": [],
                    "quality_signals": {},
                }

                result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    Path("/tmp/fake-semantic-source.pdf"),
                    metadata,
                    status,
                )

        self.assertTrue(result["ok"])
        self.assertTrue(status["ok"])
        self.assertEqual(status["success_class"], "degraded_success")
        self.assertFalse(result["machine_surface_ok"])
        self.assertEqual(
            status["quality_signals"]["primary_surface"],
            result,
        )
        self.assertTrue(
            any(
                "cjk_inline_math_source_appendix_bindings" in warning
                for warning in status["warnings"]
            )
        )
        self.assertEqual(result["inline_math_source_missing"], [])
        self.assertEqual(result["inline_math_source_appendix_anchor_count"], 1)

    def test_embedded_picture_is_materialized_as_local_asset(self) -> None:
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
            "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
        )
        document = {
            "pictures": [
                {
                    "image": {
                        "uri": f"data:image/png;base64,{png}",
                    }
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result = semantic_reflow._materialize_picture_assets(
                Path(tmpdir),
                [document],
            )

            path = Path(tmpdir) / "pictures" / "picture_1.png"
            self.assertEqual(result, {"written": 1, "skipped": 0})
            self.assertTrue(path.is_file())
            self.assertEqual(
                document["pictures"][0]["_semantic_picture_path"],
                "pictures/picture_1.png",
            )

    def test_pdf_symbol_glyphs_are_restored_semantically(self) -> None:
        # Font-private CIDs are not portable semantic symbols.  Unknown
        # values are removed from machine text and retained for source-visual
        # review by the caller.
        self.assertEqual(semantic_reflow._clean_glyph_text("(cid:52)"), "")

    def test_bert_classifier_noise_is_not_treated_as_algorithm_formula(self) -> None:
        self.assertFalse(
            adapter._looks_like_algorithm_formula(
                r"\begin{array}{l} Input = [CLS] Output = [SEP] \end{array}"
            )
        )

    def test_detached_diacritics_and_math_overlays_are_recombined(self) -> None:
        rendered = semantic_reflow._normalize_detached_diacritics(
            "⃗ x 0 ̸= 1 X ̸∈ S Richt´ arik P˘ atra¸ scu D¨ utting "
            "Pglyph[suppress] L-condition Paglyph[suppress] lka"
        )

        self.assertEqual(
            rendered,
            "x⃗ 0 ≠ 1 X ∉ S Richtárik Pătraşcu Dütting "
            "Pglyph[suppress] L-condition Paglyph[suppress] lka",
        )
        self.assertEqual(
            semantic_reflow._normalize_detached_diacritics(rendered),
            rendered,
        )

    def test_inline_citations_link_numeric_and_author_year_relations(self) -> None:
        references = [
            (1, "A. Author. First paper. 2020."),
            (2, "A. Khaled and C. Jin. Faster optimization. 2023."),
        ]

        rendered = semantic_reflow._inline_replacements(
            "See [1] and Khaled and Jin (2023).",
            references,
            [],
            markdown=False,
        )

        self.assertIn('href="#ref-1">1</a>', rendered)
        self.assertIn(
            'href="#ref-2">Khaled and Jin (2023)</a>',
            rendered,
        )

        split_reference = semantic_reflow._inline_replacements(
            "See (Bach and Moulines, 2011).",
            [
                (1, "F. Bach and É. Moulines. Non-asymptotic analysis"),
                (2, "for machine learning. NeurIPS, 2011."),
            ],
            [],
            markdown=False,
        )
        self.assertIn(
            'href="#ref-1">(Bach and Moulines, 2011)</a>',
            split_reference,
        )

        repeated_author = semantic_reflow._inline_replacements(
            "Defazio et al. (2014)",
            [
                (1, "Defazio. A practical method. 2016."),
                (2, "Defazio, Bach, and Lacoste-Julien. SAGA. 2014."),
            ],
            [],
            markdown=False,
        )
        self.assertIn('href="#ref-2">Defazio et al. (2014)</a>', repeated_author)

        square_year = semantic_reflow._inline_replacements(
            "Demené et al. [2015]",
            [(1, "Demené et al. Spatiotemporal clutter filtering. 2015.")],
            [],
            markdown=False,
        )
        self.assertIn('href="#ref-1">Demené et al. [2015]</a>', square_year)

        square_group = semantic_reflow._inline_replacements(
            "[Blattmann et al. 2023; Wan et al. 2025; Unknown et al. 2024]",
            [
                (1, "Blattmann et al. Stable Video Diffusion. 2023."),
                (2, "Wan et al. Video generation. 2025."),
            ],
            [],
            markdown=False,
        )
        self.assertIn(
            '[<a class="citation" href="#ref-1">Blattmann et al. 2023</a>; '
            '<a class="citation" href="#ref-2">Wan et al. 2025</a>; '
            "Unknown et al. 2024]",
            square_group,
        )

        suffixed_year = semantic_reflow._inline_replacements(
            "Peters et al. [2018b]",
            [
                (1, "Peters et al. First study. 2018a."),
                (2, "Peters et al. Second study. 2018b."),
            ],
            [],
            markdown=False,
        )
        self.assertIn('href="#ref-2">Peters et al. [2018b]</a>', suffixed_year)

        generalized_styles = semantic_reflow._inline_replacements(
            (
                "NVIDIA Corporation [2026]; Golub and Van Loan [2013]; "
                "Nedić et al. (2017); Kim et al. 2025"
            ),
            [
                (1, "NVIDIA Corporation. cuSOLVER Library, 2026."),
                (2, "Gene H Golub and Charles F Van Loan. Matrix computations. 2013."),
                (
                    3,
                    "Nedic, A., Olshevsky, A., and Shi, W. "
                    "(2017). Distributed optimization.",
                ),
                (
                    4,
                    "Nedić, A., Olshevsky, A., and Uribe, C. "
                    "(2017). Distributed learning.",
                ),
                (
                    5,
                    "Hoiyeong Jin, Hyojin Jang, Jeongho Kim, and Jaegul Choo. "
                    "2025. Insert Anywhere.",
                ),
                (
                    6,
                    "GeonungKim, Janghyeok Han, and Sunghyun Cho. "
                    "2025. Video From 3D.",
                ),
            ],
            [],
            markdown=False,
        )
        self.assertIn(
            'href="#ref-1">Corporation [2026]</a>',
            generalized_styles,
        )
        self.assertIn(
            'href="#ref-2">Golub and Van Loan [2013]</a>',
            generalized_styles,
        )
        self.assertIn(
            'href="#ref-4">Nedić et al. (2017)</a>',
            generalized_styles,
        )
        self.assertIn(
            'href="#ref-6">Kim et al. 2025</a>',
            generalized_styles,
        )

    def test_reference_page_header_does_not_end_bibliography(self) -> None:
        def item(kind: str, text: str, rank: float) -> semantic_reflow.FlowItem:
            return semantic_reflow.FlowItem(
                kind=kind,
                node={"text": text},
                rank=rank,
                page_no=1,
                bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 0.0},
                prov={"page_no": 1, "bbox": {}},
                source_text=text,
            )

        first = item(
            "list_item",
            "Simon Batzner et al. Equivariant networks. 2022.",
            2.0,
        )
        second = item(
            "list_item",
            "Yair Schiff et al. Caduceus. In Proceedings, volume 235",
            4.0,
        )
        continuation = item(
            "list_item",
            "of Proceedings of Machine Learning Research, PMLR, 2024.",
            5.0,
        )
        page_header = item("heading", "Li and Cheng", 3.0)
        items = [
            item("heading", "References", 1.0),
            first,
            page_header,
            second,
            continuation,
        ]

        references, texts = semantic_reflow._reference_items(items)

        self.assertEqual(references[id(first)], 1)
        self.assertEqual(references[id(second)], 2)
        self.assertNotIn(id(continuation), references)
        self.assertEqual(continuation.kind, "reference_continuation")
        self.assertEqual(page_header.kind, "reference_page_header")
        self.assertEqual(len(texts), 2)
        self.assertIn("PMLR, 2024.", texts[1][1])

    def test_repeated_page_edge_heading_is_removed_as_running_header(self) -> None:
        def heading(text: str, page: int, top: float) -> semantic_reflow.FlowItem:
            bbox = {
                "l": 200.0,
                "r": 350.0,
                "t": top,
                "b": top - 8.0,
            }
            return semantic_reflow.FlowItem(
                kind="heading",
                node={"text": text},
                rank=float(page),
                page_no=page,
                bbox=bbox,
                prov={
                    "page_no": page,
                    "bbox": {**bbox, "coord_origin": "BOTTOMLEFT"},
                },
            )

        repeated_one = heading("Li and Cheng", 1, 755.0)
        repeated_two = heading("Li and Cheng", 2, 755.0)
        real_heading = heading("Related Work", 2, 620.0)
        document = {
            "pages": {
                "1": {"size": {"width": 612.0, "height": 792.0}},
                "2": {"size": {"width": 612.0, "height": 792.0}},
            }
        }

        result = semantic_reflow._sort_items(
            [repeated_one, repeated_two, real_heading],
            document,
        )

        self.assertEqual(result, [real_heading])

    def test_footnote_relation_skips_matching_section_number(self) -> None:
        callout = semantic_reflow.FlowItem(
            "text", {}, 1.0, 1, {}, {}, "travel salesman problem 5"
        )
        heading = semantic_reflow.FlowItem(
            "heading", {"text": "5 Conclusions"}, 2.0, 1, {}, {}, ""
        )
        footnote = semantic_reflow.FlowItem(
            "footnote", {}, 3.0, 1, {}, {}, "5 https://example.test"
        )

        footnotes, callouts = semantic_reflow._footnote_relations(
            [callout, heading, footnote],
            {},
        )

        self.assertIn(id(footnote), footnotes)
        self.assertEqual(callouts[id(callout)], [("5", "5-1")])
        self.assertNotIn(id(heading), callouts)

    def test_footnote_relation_can_target_following_table_caption(self) -> None:
        footnote = semantic_reflow.FlowItem(
            "footnote", {}, 1.0, 1, {}, {}, "8 See the benchmark FAQ"
        )
        table = semantic_reflow.FlowItem(
            "table",
            {"captions": [{"$ref": "#/texts/0"}]},
            2.0,
            1,
            {},
            {},
        )
        document = {
            "texts": [
                {
                    "text": (
                        "Table 1: Results. 8 BERT is evaluated without WNLI."
                    )
                }
            ]
        }

        _footnotes, callouts = semantic_reflow._footnote_relations(
            [footnote, table],
            {},
            document,
        )

        self.assertEqual(callouts[id(table)], [("8", "8-1")])

    def test_footnote_relation_normalizes_star_glyph_variants(self) -> None:
        authors = semantic_reflow.FlowItem(
            "text", {}, 1.0, 1, {}, {}, "Qiuchen Tian ∗ Li Chai ∗ Jinming Xu ∗"
        )
        footnote = semantic_reflow.FlowItem(
            "footnote", {}, 2.0, 1, {}, {}, "⋆ Corresponding author: Li Chai."
        )

        footnotes, callouts = semantic_reflow._footnote_relations(
            [authors, footnote],
            {},
        )

        self.assertEqual(footnotes[id(footnote)][0], "star-1")
        self.assertEqual(callouts[id(authors)], [("∗", "star-1")])

    def test_table_note_relation_links_symbol_to_explanatory_note(self) -> None:
        table = semantic_reflow.FlowItem("table", {}, 1.0, 1, {}, {})
        spacer = semantic_reflow.FlowItem("heading", {}, 2.0, 1, {}, {}, "")
        note = semantic_reflow.FlowItem(
            "text",
            {},
            3.0,
            1,
            {},
            {},
            "∗ Simulated Annealing is a stochastic optimizer",
        )

        notes, callouts = semantic_reflow._table_note_relations(
            [table, spacer, note]
        )

        self.assertEqual(
            notes[id(note)],
            ("table-1", "∗", "Simulated Annealing is a stochastic optimizer"),
        )
        self.assertEqual(callouts[id(table)], [("*", "table-1")])

    def test_algorithm_and_python_emphasis_remain_semantic_html(self) -> None:
        algorithm = semantic_reflow._highlight_algorithm_html(
            "1   Input: x⃗_1^0 ∈ ℝ^d\n2   if x then // keep it"
        )
        python = semantic_reflow._highlight_python_html(
            "def solve(x):\n    # preserve comment\n    return x + 1\n"
        )

        self.assertIn('<strong class="alg-keyword">Input</strong>', algorithm)
        self.assertIn('<em class="alg-comment">// keep it</em>', algorithm)
        self.assertIn('class="alg-symbol"', algorithm)
        self.assertIn(">x⃗</span>", algorithm)
        self.assertIn("<sub>1</sub><sup>0</sup>", algorithm)
        self.assertIn(">ℝ</span>", algorithm)
        self.assertIn('<span class="code-keyword">def</span>', python)
        self.assertIn('<span class="code-comment"># preserve comment</span>', python)
        formatted_algorithm = semantic_reflow._highlight_algorithm_html(
            "Require: x_out ∈ ℝ^(B×L×D)\n1 T^(k−1) ← x_out"
        )
        self.assertIn('<strong class="alg-keyword">Require</strong>', formatted_algorithm)
        self.assertIn("x<sub>out</sub>", formatted_algorithm)
        self.assertIn("T<sup>(k", formatted_algorithm)
        self.assertIn("ℝ</span><sup>(B", formatted_algorithm)

    def test_formula_array_with_many_single_letter_terms_is_not_erased(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return 5

            def text(self, _prov, *, padding=0.0):
                return (
                    "E ∥∇fξ(x) − ∇f(x) − ∇fξ(x⋆)∥² "
                    "≤ δ²∥x − x⋆∥², ∀x ∈ Rᵈ. (5)"
                )

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\begin{array}{r}"
                    r"{E_{\xi\sim\mathcal D}["
                    r"\|\nabla f_\xi(x)-\nabla f(x)-\nabla f_\xi(x_\star)\|^2]"
                    r"\leq\delta^2\|x-x_\star\|^2,\quad"
                    r"\forall x\in\mathbb R^d.}\end{array}(5)"
                )
            },
            rank=1.0,
            page_no=4,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 4, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, 5)
        self.assertIn(r"\nabla f_\xi(x)", tex)
        self.assertIn(r"\leq\delta^2", tex)
        self.assertNotEqual(tex, r"\begin{array}{r}\end{array}")
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_standalone_equation_number_is_attached_to_overlapping_formula(self) -> None:
        class FormulaSource:
            def text(self, _prov, *, padding=0.0):
                return ""

        document = {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/texts/1"},
                ]
            },
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 100, "r": 300, "t": 500, "b": 470},
                        }
                    ],
                },
                {
                    "label": "formula",
                    "text": "( 1 7 )",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 540, "r": 558, "t": 487, "b": 478},
                        }
                    ],
                },
            ],
        }

        items = semantic_reflow._collect_items(document, FormulaSource())

        formulas = [item for item in items if item.kind == "formula"]
        self.assertEqual(len(formulas), 1)
        self.assertEqual(formulas[0].node["_semantic_equation_number"], "17")

    def test_tiny_standalone_variable_is_not_dropped_as_formula_fragment(self) -> None:
        class FormulaSource:
            def text(self, _prov, *, padding=0.0):
                return ""

        def formula(text: str) -> dict[str, object]:
            return {
                "label": "formula",
                "text": text,
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 10, "r": 14, "t": 30, "b": 20},
                    }
                ],
            }

        dropped: list[dict[str, object]] = []
        items = semantic_reflow._collect_items(
            {
                "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
                "texts": [formula("x"), formula(r"^{ -1 }")],
            },
            FormulaSource(),
            dropped_formula_artifacts=dropped,
        )

        self.assertEqual(
            ["x", r"^{ -1 }"],
            [item.node["text"] for item in items if item.kind == "formula"],
        )
        self.assertEqual([], dropped)

    def test_formula_tex_does_not_merge_distinct_spaced_numbers(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={"text": r"\begin{array}{ll}10 20 & 30 40\end{array}"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn("10 20", tex)
        self.assertIn("30 40", tex)

    def test_formula_mathml_renders_alignment_ampersands_structurally(self) -> None:
        mathml = semantic_reflow._formula_mathml(
            r"\|p(x)-p(y)\|^2 &="
            r"\Pr(X<k)\|[x-y]-\gamma[\nabla h(p(x))-\nabla h(p(y))]\|^2"
            r"\\ &="
            r"\|x-y\|^2+\gamma^2\|\nabla h(p(x))-\nabla h(p(y))\|^2"
        )

        self.assertIsNotNone(mathml)
        self.assertNotIn("<mi>&</mi>", mathml or "")
        self.assertNotIn("<mi>&amp;</mi>", mathml or "")
        self.assertIn("<mtable>", mathml or "")

    def test_formula_mathml_structures_stackrel_and_continuation_alignment(self) -> None:
        mathml = semantic_reflow._formula_mathml(
            r"A & \stackrel{(a)}{=} B \\ & = C"
        )

        self.assertIsNotNone(mathml)
        self.assertNotIn("<mi>&amp;</mi>", mathml or "")
        self.assertIn("<mtable>", mathml or "")

    def test_formula_mathml_preserves_escaped_literal_ampersand(self) -> None:
        mathml = semantic_reflow._formula_mathml(r"A\&B")

        self.assertIsNotNone(mathml)
        self.assertIn("<mi>&#x00026;</mi>", mathml or "")

    def test_formula_preserves_unseen_duplicate_rows_after_closed_array(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

            def text(self, _prov, *, padding=0.0):
                return ""

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": r"\begin{array}{rl}{u}&={v}\\{u}&={v}\end{array}",
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 20.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIsNone(number)
        self.assertEqual(tex.count(r"\end{array}"), 1)
        self.assertEqual(tex.count("u"), 2)
        mathml = semantic_reflow._formula_mathml(tex)
        self.assertIsNotNone(mathml)
        self.assertNotIn("<mi>&amp;</mi>", mathml or "")

    def test_formula_preserves_unseen_preceding_prose_in_math_node(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

            def text(self, _prov, *, padding=0.0):
                return "For lambda less than ell we have"

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\text {$\forall < \ell$ we have} \\ "
                    r"\frac{\ell^2}{\lambda^2}"
                    r"\frac{\Pr(\text{Poisson}(\lambda)>k)}"
                    r"{\Pr(\text{Poisson}(\lambda)<k)}"
                    r"&=\frac{\sum_{j>k}\lambda^{j-2}/j!}"
                    r"{\sum_{j>k}\ell^{j-2}/j!}"
                )
            },
            rank=1.0,
            page_no=38,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 38, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn(r"\text {$\forall < \ell$ we have}", tex)
        self.assertTrue(tex.startswith(r"\text {$\forall < \ell$ we have}"))

    def test_unseen_equation_number_eight_preserves_original_tex(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return 8

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": r"A_t - B_t = C_t",
            },
            rank=1.0,
            page_no=7,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 7, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, 8)
        self.assertEqual(tex, r"A_t - B_t = C_t")
        self.assertNotIn("<img", tex)

    def test_unseen_equation_number_eleven_preserves_signature_like_tex(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return 11

        original = r"\sigma _ { k + 1 } \leq A _ { 2 } + B _ { 2 }"
        item = semantic_reflow.FlowItem(
            kind="formula",
            node={"text": original},
            rank=1.0,
            page_no=7,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 7, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, 11)
        self.assertEqual(tex, original)

    def test_unseen_equation_number_three_preserves_original_tex(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return 3

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={"text": r"z_3 = a_3 + b_3"},
            rank=1.0,
            page_no=5,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 5, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, 3)
        self.assertIn(r"z_3 = a_3 + b_3", tex)
        self.assertNotIn(r"\mathrm{rabbit}", tex)
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_unseen_equation_number_six_preserves_original_tex(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return 6

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={"text": r"q_6 = u_6 - v_6"},
            rank=1.0,
            page_no=5,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 5, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, 6)
        self.assertIn(r"q_6 = u_6 - v_6", tex)
        self.assertNotIn("overbrace", tex)

    def test_decimal_equation_label_is_removed_from_formula_body_and_preserved(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return "2.1"

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\psi(x)=\frac{x^\ell(1-x)^{n-\ell}}{B(\ell,n-\ell)}"
                    r"\quad ( 2 . 1 )"
                )
            },
            rank=1.0,
            page_no=6,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 6, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, "2.1")
        self.assertNotIn("( 2 . 1 )", tex)
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_formula_removes_internal_markup_but_preserves_unseen_spaced_prose(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\begin{array}{rl}"
                    r"{ T h e q u a n t i t i e s a l w a y s s a t i s f y }\\"
                    r"{ w e h a v e }\\"
                    r"{ \alpha_{i+1}=a_i }"
                    r"\end{array}"
                    r"<formula><loc_1><loc_2>leaked internal payload"
                )
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn("T h e q", tex)
        self.assertIn("w e h", tex)
        self.assertNotIn("<formula>", tex)
        self.assertIn(r"\alpha_{i+1}=a_i", tex)
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_formula_prefers_richer_docling_array_payload_over_truncated_prefix(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"a=b <formula><loc_1><loc_2>"
                    r"\begin{array}{rl}a&=b\\"
                    r"c&=d\\e&=f\end{array}"
                )
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn("a", tex)
        self.assertIn("c", tex)
        self.assertIn("d", tex)
        self.assertIn("e", tex)
        self.assertIn("f", tex)
        self.assertNotIn("<formula>", tex)

    def test_formula_repairs_unbalanced_left_right_for_mathml(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return "4.1"

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"F_q(x)=\left\{\begin{array}{ll}"
                    r"1 & x>0\\0 & x\leq0"
                    r"\end{array}"
                )
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, "4.1")
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_formula_repairs_truncated_array_and_stray_alignment_brace(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\begin{array}{rl}{a}&\leq }&{b}\\"
                    r"{c}&={d}\end{array"
                )
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn(r"\end{array}", tex)
        self.assertNotIn(r"\leq }&", tex)
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_formula_preserves_unseen_empty_and_repeated_rows(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

        repeated = r"&{\leq x}"
        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\begin{array}{rl}{a}&={b}\\"
                    r"&{\,}\\"
                    + repeated
                    + r"\\"
                    + repeated
                    + r"\end{array}"
                )
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn(r"{\,}", tex)
        self.assertEqual(tex.count(repeated), 2)
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_formula_preserves_unseen_long_signature_like_tex(self) -> None:
        class FormulaSource:
            def equation_number(self, _prov):
                return None

            def text(self, _prov, *, padding=0.0):
                return "E σ k+1 ∑ w k+1 − x ⋆"

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={
                "text": (
                    r"\begin{array}{rl}{\text{here}, A _ { 1 } = 0}"
                    + (r"\\&{x}" * 800)
                    + r"\end{array}"
                )
            },
            rank=1.0,
            page_no=44,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 44, "bbox": {}},
        )

        tex, _number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertIn(r"\text{here}", tex)
        self.assertIn(r"A _ { 1 } = 0", tex)
        self.assertEqual(tex.count(r"&{x}"), 800)
        self.assertNotIn(r"\sum_{i=1}^{n}", tex)
        self.assertNotIn(r"\mathbb{E}", tex)
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_markdown_table_preserves_internal_line_breaks(self) -> None:
        rendered = semantic_reflow._markdown_table(
            [["Column"], ["first line\nsecond line"]]
        )

        self.assertIn("first line<br>second line", rendered)

    def test_table_grid_preserves_numeric_four_and_five_cells(self) -> None:
        cells = []
        values = [["Method", "Score", "Count"], ["A", "4", "5"]]
        for row, values_row in enumerate(values):
            for col, value in enumerate(values_row):
                cells.append(
                    {
                        "start_row_offset_idx": row,
                        "start_col_offset_idx": col,
                        "text": value,
                    }
                )
        item = semantic_reflow.FlowItem(
            kind="table",
            node={
                "data": {
                    "num_rows": 2,
                    "num_cols": 3,
                    "table_cells": cells,
                }
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
            source_text="",
        )

        grid, count = semantic_reflow._table_grid(object(), item)

        self.assertEqual(count, 1)
        self.assertEqual(grid[1], ["A", "4", "5"])

    def test_source_caption_recovers_table_title_from_geometry(self) -> None:
        class CaptionSource:
            def page_size(self, _page_no):
                return 612.0, 792.0

            def text(self, prov, *, padding=0.0):
                bbox = prov["bbox"]
                if bbox["b"] >= 700:
                    return "Table 4: Upper and lower bounds of all parameters"
                return ""

        item = semantic_reflow.FlowItem(
            kind="table",
            node={},
            rank=1.0,
            page_no=16,
            bbox={"l": 128.0, "r": 484.0, "t": 700.0, "b": 479.0},
            prov={
                "page_no": 16,
                "bbox": {
                    "l": 128.0,
                    "r": 484.0,
                    "t": 700.0,
                    "b": 479.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            },
        )

        caption = semantic_reflow._source_caption(
            CaptionSource(),
            item,
            kind="table",
        )

        self.assertEqual(
            caption,
            "Table 4: Upper and lower bounds of all parameters",
        )

    def test_source_caption_converts_topleft_edges_without_swapping_them(self) -> None:
        class CaptionSource:
            def page_size(self, _page_no):
                return 612.0, 800.0

            def __init__(self):
                self.probes = []

            def text(self, prov, *, padding=0.0):
                bbox = prov["bbox"]
                self.probes.append((bbox["t"], bbox["b"]))
                if bbox["t"] == 752.0 and bbox["b"] == 700.0:
                    return "Table 9: Coordinate-origin regression"
                return ""

        source = CaptionSource()
        item = semantic_reflow.FlowItem(
            kind="table",
            node={},
            rank=1.0,
            page_no=3,
            bbox={"l": 10.0, "r": 100.0, "t": 100.0, "b": 120.0},
            prov={
                "page_no": 3,
                "bbox": {
                    "l": 10.0,
                    "r": 100.0,
                    "t": 100.0,
                    "b": 120.0,
                    "coord_origin": "TOPLEFT",
                },
            },
        )

        caption = semantic_reflow._source_caption(source, item, kind="table")

        self.assertEqual(caption, "Table 9: Coordinate-origin regression")
        self.assertEqual(source.probes[0], (752.0, 700.0))

    def test_review_screenshots_are_removed_from_primary_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "document.html").write_text(
                (
                    "<p>semantic body</p>"
                    '<div class="docling-formula-source">'
                    '<a href="formulas/formula_1.png">source image</a></div>'
                    '<section class="docling-table-source-evidence-appendix">'
                    '<img src="tables/table_1.png"></section>'
                    '<section class="docling-formula-source-evidence-appendix">'
                    '<img src="formulas/formula_1.png"></section>'
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    "semantic body\n\n"
                    "## Original table renderings\n\n"
                    "![Table](tables/table_1.png)\n\n"
                    "## Original formula renderings\n\n"
                    "![Formula](formulas/formula_1.png)\n"
                ),
                encoding="utf-8",
            )

            counts = (
                semantic_reflow._remove_review_evidence_from_primary_surfaces(
                    output_dir
                )
            )

            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertEqual(counts["html_appendices_removed"], 2)
            self.assertEqual(counts["html_formula_source_links_removed"], 1)
            self.assertEqual(counts["markdown_appendices_removed"], 2)
            self.assertEqual(html_text, "<p>semantic body</p>")
            self.assertEqual(markdown_text, "semantic body\n")

    def test_legacy_cjk_formulas_use_native_mathml_without_mathjax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "document.html").write_text(
                (
                    "<!doctype html><html><head>"
                    '<style id="docling-formula-second-pass-style">legacy</style>'
                    '<script id="docling-formula-second-pass-mathjax">legacy</script>'
                    '<script defer src="https://cdn.jsdelivr.net/npm/'
                    'mathjax@3/es5/tex-svg.js"></script></head><body>'
                    "<p>中文正文保持不变。</p>"
                    '<div class="docling-formula-second-pass" '
                    'data-formula-index="1" data-formula-status="cn_final_polish">'
                    '<div class="docling-formula-second-pass-label">'
                    "Formula 1 patched by formula second pass</div>"
                    '<div class="docling-formula-render">'
                    r"\[x_1=y\quad(1)\]</div>"
                    '<pre class="docling-formula-tex">'
                    r"x_1=y\quad(1)</pre></div>"
                    '<div class="docling-formula-second-pass" '
                    'data-formula-index="2" data-formula-status="cn_final_polish">'
                    '<div class="docling-formula-second-pass-label">'
                    "Formula 2 patched by formula second pass</div>"
                    '<div class="docling-formula-render">'
                    r"\[\frac{a}{b}=c\quad(2)\]</div>"
                    '<pre class="docling-formula-tex">'
                    r"\frac{a}{b}=c\quad(2)</pre></div>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    "中文正文保持不变。\n\n"
                    r"$$x_1=y\quad(1)$$"
                    "\n\n"
                    r"$$\frac{a}{b}=c\quad(2)$$"
                    "\n"
                ),
                encoding="utf-8",
            )

            result = semantic_reflow._normalize_legacy_formula_surfaces(
                output_dir
            )
            html_text = (output_dir / "document.html").read_text(
                encoding="utf-8"
            )
            markdown_text = (output_dir / "document.md").read_text(
                encoding="utf-8"
            )

        self.assertTrue(result["applied"])
        self.assertEqual(result["formula_count"], 2)
        self.assertEqual(result["mathml_count"], 2)
        self.assertEqual(result["tex_fallback_count"], 0)
        self.assertIn("中文正文保持不变。", html_text)
        self.assertEqual(html_text.count('<div class="formula"'), 2)
        self.assertEqual(html_text.count("data-formula-index"), 2)
        self.assertEqual(html_text.count("<math "), 2)
        self.assertIn('<span class="equation-number">(1)</span>', html_text)
        self.assertIn("<details><summary>LaTeX</summary>", html_text)
        self.assertIn("<!-- source-formula-anchor:1 -->", html_text)
        self.assertIn("<!-- source-formula-anchor:2 -->", html_text)
        self.assertNotIn("docling-formula-second-pass", html_text)
        self.assertNotIn("MathJax", html_text)
        self.assertNotIn("cdn.jsdelivr.net", html_text)
        self.assertIn("$$\nx_1=y\\tag{1}\n$$", markdown_text)
        self.assertIn("$$\n\\frac{a}{b}=c\\tag{2}\n$$", markdown_text)
        self.assertIn(
            "$$\nx_1=y\\tag{1}\n$$\n<!-- source-formula-anchor:1 -->",
            markdown_text,
        )
        self.assertIn(
            "$$\n\\frac{a}{b}=c\\tag{2}\n$$\n<!-- source-formula-anchor:2 -->",
            markdown_text,
        )

    def test_legacy_formula_normalization_rejects_surface_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            original_html = (
                "<html><head></head><body>"
                '<div class="docling-formula-second-pass" '
                'data-formula-index="1">'
                '<pre class="docling-formula-tex">x=y\\quad(1)</pre>'
                "</div></body></html>"
            )
            (output_dir / "document.html").write_text(
                original_html,
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$a=b\\quad(1)$$\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "formula content mismatch",
            ):
                semantic_reflow._normalize_legacy_formula_surfaces(output_dir)

            self.assertEqual(
                (output_dir / "document.html").read_text(encoding="utf-8"),
                original_html,
            )

    def test_legacy_formula_normalization_does_not_invent_equation_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "document.html").write_text(
                "<html><head></head><body>"
                '<div class="docling-formula-second-pass" data-formula-index="1">'
                '<pre class="docling-formula-tex">x=y</pre>'
                "</div></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x=y$$\n", encoding="utf-8"
            )

            result = semantic_reflow._normalize_legacy_formula_surfaces(
                output_dir
            )
            html_text = (output_dir / "document.html").read_text(
                encoding="utf-8"
            )
            markdown_text = (output_dir / "document.md").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result["equation_numbers"], [])
        self.assertNotIn('<span class="equation-number">', html_text)
        self.assertNotIn(r"\tag{1}", markdown_text)

    def test_algorithm_preformatted_block_preserves_geometry_indentation(self) -> None:
        class AlgorithmSource:
            def lines(self, _prov, *, padding=0.0):
                return [
                    {"text": "Algorithm 2: Example", "x0": 10, "chars": []},
                    {"text": "1 if ready then", "x0": 10, "chars": []},
                    {"text": "2 act()", "x0": 28, "chars": []},
                    {"text": "3 end if", "x0": 10, "chars": []},
                ]

        item = semantic_reflow.FlowItem(
            kind="algorithm",
            node={},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        title, block = semantic_reflow._preformatted_block(
            AlgorithmSource(),
            item,
            algorithm=True,
        )

        self.assertEqual(title, "Algorithm 2: Example")
        self.assertIn("1   if ready then", block)
        self.assertIn("2       act()", block)
        self.assertIn("3   end if", block)

    def test_algorithm_table_splits_merged_lines_and_preserves_indentation(self) -> None:
        class AlgorithmSource:
            def lines(self, _prov, *, padding=0.0):
                return []

        def cell(row, col, text, left):
            return {
                "start_row_offset_idx": row,
                "start_col_offset_idx": col,
                "text": text,
                "bbox": {"l": left},
            }

        item = semantic_reflow.FlowItem(
            kind="algorithm",
            node={
                "data": {
                    "num_rows": 5,
                    "num_cols": 2,
                    "table_cells": [
                        cell(0, 0, "Algorithm 1 Example", 10),
                        cell(
                            1,
                            0,
                            "Require: Input x; Ensure: x ∈ B × L × D",
                            10,
                        ),
                        cell(2, 0, "1: 2:", 10),
                        cell(2, 1, "build(x) for k = 1 to L - 1 do", 20),
                        cell(3, 0, "3:", 10),
                        cell(3, 1, "act()", 36),
                        cell(4, 0, "4:", 10),
                        cell(4, 1, "end for", 20),
                    ],
                }
            },
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
            source_text=(
                "Algorithm 1 Example\n"
                "Require: Input x; exposed generator set S = {s , . . . , s }\n"
                "1 q\n"
                "Ensure: x ∈ RB×L×D\n"
                "out\n"
                "1: init ← build(x)\n"
                "2: for k = 1 to L - 1 do\n"
                "3: act()\n"
                "4: end for"
            ),
        )

        title, block = semantic_reflow._preformatted_block(
            AlgorithmSource(),
            item,
            algorithm=True,
        )

        self.assertEqual(title, "Algorithm 1 Example")
        self.assertIn("Require: Input x", block)
        self.assertIn("Ensure: x ∈ B × L × D", block)
        self.assertIn("1   init ← build(x)", block)
        self.assertIn("2   for k = 1 to L - 1 do", block)
        self.assertIn("3       act()", block)
        self.assertIn("4   end for", block)

    def test_algorithm_heading_and_list_group_become_one_semantic_block(self) -> None:
        class AlgorithmSource:
            def text(self, _prov):
                return ""

        document = {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/groups/0"},
                ]
            },
            "texts": [
                {
                    "label": "section_header",
                    "text": "Algorithm 3 Example",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {
                                "l": 40,
                                "r": 300,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
                {
                    "label": "list_item",
                    "text": "1: for k = 1, 2, ... do",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 50, "r": 300, "t": 675, "b": 660},
                        }
                    ],
                },
                {
                    "label": "list_item",
                    "text": "2: end for",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 50, "r": 160, "t": 655, "b": 640},
                        }
                    ],
                },
            ],
            "groups": [
                {
                    "children": [
                        {"$ref": "#/texts/1"},
                        {"$ref": "#/texts/2"},
                    ]
                }
            ],
        }

        items = semantic_reflow._collect_items(document, AlgorithmSource())

        algorithms = [item for item in items if item.kind == "algorithm"]
        self.assertEqual(len(algorithms), 1)
        self.assertEqual(algorithms[0].node["self_ref"], "#/texts/0")
        self.assertEqual(
            semantic_reflow._numbered_algorithm_lines(algorithms[0].node["text"])[1],
            [
                (1, "for k = 1, 2, ... do"),
                (2, "end for"),
            ],
        )
        self.assertFalse(any(item.kind in {"heading", "list_item"} for item in items))

    def test_algorithm_heading_and_following_code_become_one_algorithm(self) -> None:
        class AlgorithmSource:
            def text(self, prov):
                bbox = prov.get("bbox") or {}
                if bbox.get("t") == 580:
                    return "Following explanation."
                return ""

        document = {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/texts/1"},
                    {"$ref": "#/texts/2"},
                ]
            },
            "texts": [
                {
                    "label": "section_header",
                    "text": "Algorithm 2 Quantile algorithm",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {
                                "l": 40,
                                "r": 300,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
                {
                    "label": "code",
                    "text": "Input: partition\nfor i in [n] do\n  Accept item i\nend for",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 45, "r": 300, "t": 675, "b": 600},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "Following explanation.",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 40, "r": 300, "t": 580, "b": 560},
                        }
                    ],
                },
            ],
        }

        items = semantic_reflow._collect_items(document, AlgorithmSource())

        self.assertEqual([item.kind for item in items], ["algorithm", "text"])
        self.assertEqual(items[0].node["self_ref"], "#/texts/0")
        self.assertIn("Input: partition", items[0].node["text"])
        self.assertEqual(items[1].node["text"], "Following explanation.")

    def test_algorithm_split_formula_steps_are_recovered_from_source_rectangle(self) -> None:
        complete_source_text = (
            "Algorithm 8 Point SAGA "
            "1: Parameters: learning rate γ > 0 "
            "2: for k = 0, 1, 2, ... do "
            "3: Sample i_k uniformly "
            "4: Set h_k = ∇f_i(w_i) "
            "5: x_{k+1} = prox(x_k + γh_k) "
            "6: Set w_j^{k+1} by cases "
            "7: end for"
        )

        class AlgorithmSource:
            def text(self, _prov):
                return complete_source_text

        def prov(top: int, bottom: int):
            return [
                {
                    "page_no": 43,
                    "bbox": {
                        "l": 40,
                        "r": 300,
                        "t": top,
                        "b": bottom,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ]

        document = {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/groups/0"},
                    {"$ref": "#/formulas/0"},
                    {"$ref": "#/formulas/1"},
                    {"$ref": "#/texts/3"},
                    {"$ref": "#/formulas/2"},
                    {"$ref": "#/groups/1"},
                    {"$ref": "#/texts/5"},
                ]
            },
            "texts": [
                {"label": "section_header", "text": "Algorithm 8 Point SAGA", "prov": prov(700, 680)},
                {"label": "list_item", "text": "1: Parameters", "prov": prov(675, 660)},
                {"label": "list_item", "text": "3: Sample i_k uniformly", "prov": prov(655, 640)},
                {"label": "text", "text": "6: Set", "prov": prov(615, 600)},
                {"label": "list_item", "text": "7: end for", "prov": prov(595, 580)},
                {"label": "section_header", "text": "Commentary:", "prov": prov(560, 545)},
            ],
            "formulas": [
                {"label": "formula", "text": r"4 \\colon h_k = \\nabla f_i", "prov": prov(635, 620)},
                {"label": "formula", "text": r"5 \\colon x_{k+1}=\\operatorname{prox}(x_k)", "prov": prov(615, 600)},
                {"label": "formula", "text": r"6 \\colon w_j^{k+1}=\\begin{cases}\\end{cases}", "prov": prov(600, 585)},
            ],
            "groups": [
                {
                    "children": [
                        {"$ref": "#/texts/1"},
                        {"$ref": "#/texts/2"},
                    ]
                },
                {"children": [{"$ref": "#/texts/4"}]},
            ],
        }

        items = semantic_reflow._collect_items(document, AlgorithmSource())

        algorithms = [item for item in items if item.kind == "algorithm"]
        self.assertEqual(len(algorithms), 1)
        _title, lines = semantic_reflow._numbered_algorithm_lines(
            algorithms[0].node["text"]
        )
        self.assertEqual([number for number, _text in lines], list(range(1, 8)))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1].node["text"], "Commentary:")

    def test_algorithm_formula_step_does_not_guess_unseen_math(self) -> None:
        parsed = semantic_reflow._algorithm_formula_step(
            r"\begin{array} { r l } { 5 \colon } & "
            r"x _ { k + 1 } = p r o x _ { \gamma f _ { i _ { k } } }"
            r"\left ( x _ { k } + \gamma h _ { k } \right ) }"
            r"\end{array}"
        )

        self.assertIsNone(parsed)

    def test_algorithm_case_assignments_preserve_unseen_text(self) -> None:
        point_saga = semantic_reflow._normalize_algorithm_semantics(
            "Set wj = k+1 k k+1 wj for j ̸ = i k k"
        )
        loopless = semantic_reflow._normalize_algorithm_semantics(
            "Set w k +1 = { x k +1 with probability p "
            "w k with probability 1 -p"
        )

        self.assertEqual(point_saga, "Set wj = k+1 k k+1 wj for j ̸ = i k k")
        self.assertEqual(
            loopless,
            "Set w k +1 = { x k +1 with probability p w k with probability 1 -p",
        )

    def test_unnumbered_algorithm_preserves_unseen_tokens_and_indentation(self) -> None:
        title, block = semantic_reflow._unnumbered_algorithm_block(
            "Algorithm 2 Quantile algorithm for ( k, ℓ ) "
            "Input: Partition ( ϵ j i ) j -1 ≤ i ≤ n of [0, 1], "
            "distribution F of the X i. "
            "j ← 1 "
            "for i ∈ [n]: do "
            "Draw q j i from Beta(ℓ, n-ℓ) truncated between "
            "ϵ j i -1 and ϵ j i "
            "if X i ≥ F -1 (1 -q j i) then "
            "Accept item i "
            "if j = k then "
            "Stop "
            "else "
            "j ← j +1 "
            "end if "
            "end if "
            "end for"
        )

        self.assertEqual(title, "Algorithm 2 Quantile algorithm for ( k, ℓ )")
        self.assertIn(r"( ϵ j i ) j -1 ≤ i ≤ n", block)
        self.assertIn(r"q j i", block)
        self.assertIn(r"ϵ j i -1", block)
        self.assertIn(r"F -1", block)
        self.assertIn("\nfor i ∈ [n]: do", block)
        self.assertIn("\n    if X i", block)
        self.assertIn("\n        if j = k then", block)
        self.assertIn("\n        else", block)
        self.assertIn("\nend for", block)


class EnglishReviewPolishTests(unittest.TestCase):
    def test_image_only_pdf_finds_same_batch_text_layer_recovery_source(self) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "born-digital.pdf"
            scan = root / "rasterized-scan.pdf"
            other = root / "wrong-page-count.pdf"

            doc = fitz.open()
            for page_no in range(2):
                page = doc.new_page(width=300, height=400)
                for line_no in range(30):
                    page.insert_text(
                        (36, 30 + line_no * 11),
                        (
                            f"Recoverable source page {page_no + 1} line {line_no}. "
                            "citation formula reference paragraph with text layer."
                        ),
                        fontsize=8,
                    )
            doc.save(source)
            doc.close()

            src_doc = fitz.open(source)
            scan_doc = fitz.open()
            for page in src_doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
                new_page = scan_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(page.rect, pixmap=pix)
            scan_doc.save(scan)
            scan_doc.close()
            src_doc.close()

            wrong_doc = fitz.open()
            wrong_doc.new_page(width=300, height=400).insert_text((36, 80), "wrong " * 300)
            wrong_doc.save(other)
            wrong_doc.close()

            recovery = adapter.find_text_layer_recovery_source(scan)

        self.assertTrue(recovery["applied"])
        self.assertEqual(Path(recovery["source_path"]).name, "born-digital.pdf")
        self.assertEqual(recovery["reason"], "same_batch_text_layer_source_matched")
        self.assertLessEqual(recovery["page_size_distance"], 2.0)

    def test_non_image_only_pdf_does_not_use_text_layer_recovery(self) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "normal.pdf"
            doc = fitz.open()
            doc.new_page(width=300, height=400).insert_text((36, 80), "normal text " * 300)
            doc.save(source)
            doc.close()

            recovery = adapter.find_text_layer_recovery_source(source)

        self.assertFalse(recovery["applied"])
        self.assertEqual(recovery["reason"], "not_image_only_pdf")

    def test_text_layer_recovery_rejects_sibling_that_differs_on_later_page(self) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "original.pdf"
            scan = root / "rasterized-scan.pdf"
            wrong = root / "wrong-later-page.pdf"

            def write_source(path: Path, *, changed: bool) -> None:
                document = fitz.open()
                for page_no in range(3):
                    page = document.new_page(width=300, height=400)
                    for line_no in range(30):
                        text = (
                            f"Recoverable source page {page_no + 1} line {line_no}. "
                            "citation formula reference paragraph with text layer."
                        )
                        if changed and page_no == 2 and line_no == 15:
                            text = (
                                "CHANGED FORMULA: P_i = WRONG / DENOMINATOR "
                                "WITH DIFFERENT SYMBOLS"
                            )
                        page.insert_text((36, 30 + line_no * 11), text, fontsize=8)
                document.save(path)
                document.close()

            write_source(original, changed=False)
            source_document = fitz.open(original)
            scan_document = fitz.open()
            for page in source_document:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
                target_page = scan_document.new_page(
                    width=page.rect.width,
                    height=page.rect.height,
                )
                target_page.insert_image(page.rect, pixmap=pixmap)
            scan_document.save(scan)
            scan_document.close()
            source_document.close()
            original.unlink()
            write_source(wrong, changed=True)

            recovery = adapter.find_text_layer_recovery_source(scan)

        self.assertFalse(recovery["applied"])
        self.assertEqual(recovery["reason"], "no_same_batch_text_layer_source")

    def test_cn_filename_does_not_select_macos_only_or_legacy_behavior(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/CN.pdf"),
            cn_ocr_parity=False,
            legacy_cn_accepted_baseline=False,
            formula_second_pass_policy="apply-all",
        )

        self.assertFalse(adapter.effective_cn_ocr_parity(args))
        self.assertFalse(adapter.is_cn_accepted_path(args))
        self.assertEqual(adapter.effective_formula_second_pass_policy(args), "apply-all")

    def test_legacy_cn_baseline_requires_explicit_compatibility_switch(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/CN.pdf"),
            cn_ocr_parity=True,
            legacy_cn_accepted_baseline=True,
            expected_input_sha256=adapter.CN_ACCEPTED_BASELINE["source_pdf_sha256"],
        )

        self.assertTrue(adapter.effective_cn_ocr_parity(args))
        self.assertTrue(adapter.is_cn_accepted_path(args))

    def test_legacy_cn_baseline_uses_submitted_name_after_input_snapshot(self) -> None:
        args = Namespace(
            input_file=Path("/private/tmp/quality-parity-input/random/input.pdf"),
            _submitted_input_name="CN.pdf",
            cn_ocr_parity=True,
            legacy_cn_accepted_baseline=True,
            expected_input_sha256=adapter.CN_ACCEPTED_BASELINE["source_pdf_sha256"],
        )

        self.assertTrue(adapter.is_cn_accepted_path(args))

    def test_legacy_cn_baseline_rejects_different_pdf_identity(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/CN.pdf"),
            cn_ocr_parity=True,
            legacy_cn_accepted_baseline=True,
            expected_input_sha256="0" * 64,
        )

        self.assertFalse(adapter.is_cn_accepted_path(args))

    def test_transformers_formula_uses_server_side_granite_preset(self) -> None:
        args = Namespace(
            formula_policy="granite_transformers",
            enable_formula_mlx=False,
            image_export_mode="referenced",
            page_start=None,
            page_end=None,
        )

        options = adapter.base_options(args, force_ocr=False)

        self.assertEqual("granite_docling", options["code_formula_preset"])
        self.assertNotIn("code_formula_custom_config", options)
        self.assertEqual(1200.0, options["document_timeout"])

    def test_docker_formula_profile_releases_backend_converter_cache(self) -> None:
        args = Namespace(
            formula_policy="formula_service",
            enable_formula_mlx=False,
            serve_url="http://backend:5001",
        )
        with patch.object(
            adapter, "get_json", return_value={"status": "success"}
        ) as get_json:
            result = adapter.release_backend_converter_cache(args)

        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        get_json.assert_called_once_with(
            "http://backend:5001/v1/clear/converters", timeout=120
        )

    def test_macos_formula_profile_keeps_backend_cache(self) -> None:
        args = Namespace(
            formula_policy="granite_mlx",
            enable_formula_mlx=False,
            serve_url="http://127.0.0.1:5001",
        )
        with patch.object(adapter, "get_json") as get_json:
            result = adapter.release_backend_converter_cache(args)

        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        get_json.assert_not_called()

    def test_portable_formula_uses_codeformulav2_default_preset(self) -> None:
        args = Namespace(
            formula_policy="codeformula_transformers",
            enable_formula_mlx=False,
            image_export_mode="referenced",
            page_start=None,
            page_end=None,
        )

        options = adapter.base_options(args, force_ocr=False)

        self.assertTrue(options["do_formula_enrichment"])
        self.assertEqual("codeformulav2", options["code_formula_preset"])
        self.assertNotIn("code_formula_custom_config", options)

    def test_custom_ocr_config_remains_typed_for_source_api(self) -> None:
        args = Namespace(cn_ocr_request_shape="custom")
        options: dict[str, object] = {}

        adapter.apply_cn_ocr_options(options, args)

        config = options["ocr_custom_config"]
        self.assertEqual("ocrmac", config["kind"])
        self.assertEqual(adapter.CN_OCR_LANG, config["lang"])

    def test_english_apply_all_policy_remains_isolated(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/two-col-arxiv-ai-lora.pdf"),
            cn_ocr_parity=False,
            formula_second_pass_policy="apply-all",
        )

        self.assertFalse(adapter.is_cn_accepted_path(args))
        self.assertEqual(adapter.effective_formula_second_pass_policy(args), "apply-all")

    def test_cn_baseline_rejects_final_output_with_too_few_visible_cn_chars(self) -> None:
        formulas = [
            {"label": "formula", "text": rf"x_{{{number}}} = y \quad ( {number} )"}
            for number in range(1, 25)
        ]
        document = {
            "texts": [
                {"label": "text", "text": "中" * adapter.CN_ACCEPTED_BASELINE["minimum_cn_character_count"]},
                *formulas,
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("abc\n", encoding="utf-8")
            (output_dir / "document.html").write_text(
                "<html><body><p>abc</p></body></html>",
                encoding="utf-8",
            )
            diagnostics = adapter.cn_accepted_baseline_diagnostics(output_dir)

        self.assertFalse(diagnostics["ok"])
        self.assertIn("final_markdown_cn_character_count=0", diagnostics["reasons"])
        self.assertIn("final_html_cn_character_count=0", diagnostics["reasons"])

    def test_cn_accepted_baseline_diagnostics(self) -> None:
        formulas = [
            {"label": "formula", "text": rf"x_{{{number}}} = y \quad ( {number} )"}
            for number in range(1, 25)
        ]
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "获取历史时刻知识状态的权重为"
                        + "知识状态与习题嵌入表示" * 1000
                    ),
                },
                *formulas,
            ]
        }
        final_text = (
            "获取历史时刻知识状态的权重为"
            + "知识状态与习题嵌入表示" * 1000
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                final_text,
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                f"<html><body><p>{final_text}</p></body></html>",
                encoding="utf-8",
            )

            diagnostics = adapter.cn_accepted_baseline_diagnostics(output_dir)

        self.assertTrue(diagnostics["ok"])
        self.assertEqual(diagnostics["gxx_count"], 0)
        self.assertEqual(diagnostics["formula_count"], 24)
        self.assertEqual(diagnostics["equation_numbers"], list(range(1, 25)))

    def test_cn_accepted_baseline_rejects_gxx_and_shifted_formula_sequence(self) -> None:
        formulas = [
            {"label": "formula", "text": rf"x_{{{number}}} = y \quad ( {number} )"}
            for number in range(1, 25)
        ]
        formulas[13]["text"] = r"x = y \quad ( 13 )"
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "/G01" + "知识状态与习题嵌入表示" * 1000,
                },
                *formulas,
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "获取历史时刻知识状态的权重为",
                encoding="utf-8",
            )

            diagnostics = adapter.cn_accepted_baseline_diagnostics(output_dir)

        self.assertFalse(diagnostics["ok"])
        self.assertIn("gxx_count=1", diagnostics["reasons"])
        self.assertIn("formula_equation_sequence_mismatch", diagnostics["reasons"])

    def test_autolinks_visible_plain_urls(self) -> None:
        html, count = adapter._autolink_plain_urls(
            '<p>Code at https://github.com/microsoft/LoRA .</p>'
        )

        self.assertEqual(count, 1)
        self.assertIn(
            '<a href="https://github.com/microsoft/LoRA">'
            "https://github.com/microsoft/LoRA</a>",
            html,
        )

    def test_links_mathml_formula_blocks_by_order(self) -> None:
        html, count = adapter.inject_formula_source_links_by_mathml_order(
            '<div><math display="block"></math></div>',
            {1: {"source": "formulas/formula_1.png", "context": "formulas/formula_1_context.png"}},
        )

        self.assertEqual(count, 1)
        self.assertIn('data-formula-index="1"', html)
        self.assertIn("formulas/formula_1_context.png", html)

    def test_footnote_diagnostics_flag_split_fragments(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Author ∗ Name",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 120,
                                "r": 124,
                                "t": 90,
                                "b": 85,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ]
        }

        diagnostics = adapter.footnote_review_diagnostics(document)

        self.assertEqual(len(diagnostics), 1)
        self.assertIn("isolated_numeric_footnote_fragment", diagnostics[0]["reasons"])
        self.assertIn("near_page_bottom_footnote", diagnostics[0]["reasons"])
        self.assertIn("anchor_content_marker_mismatch", diagnostics[0]["reasons"])

    def test_first_page_footnote_recovery_merges_hyphenated_fragments(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [{"page_no": 1, "bbox": {"l": 120, "r": 124, "t": 90, "b": 85}}],
                },
                {
                    "label": "footnote",
                    "text": "1 mance significantly as shown in Appendix A.",
                    "prov": [{"page_no": 1, "bbox": {"l": 108, "r": 271, "t": 79, "b": 60}}],
                },
                {
                    "label": "footnote",
                    "text": (
                        "Compared to V1, this draft includes better baselines, "
                        "fine-tuning boosts its perfor-"
                    ),
                    "prov": [{"page_no": 1, "bbox": {"l": 124, "r": 504, "t": 88, "b": 70}}],
                },
            ]
        }

        diagnostics = adapter.first_page_footnote_recovery_diagnostics(document)
        recoverable = [item for item in diagnostics if item.get("action") == "diagnostic_only_generic_quarantine_preferred"]
        evidence_only = [item for item in diagnostics if not item.get("safe_to_apply")]

        self.assertEqual(len(recoverable), 1)
        self.assertIn("performance significantly", recoverable[0]["recovered_text"])
        self.assertFalse(recoverable[0]["safe_to_apply"])
        self.assertEqual(evidence_only[-1]["footnote_number"], "0")

    def test_first_page_footnote_html_recovery_is_evidence_only(self) -> None:
        diagnostics = [
            {
                "page_no": 1,
                "footnote_number": "1",
                "lead_fragment": "Compared to V1, fine-tuning boosts its perfor-",
                "tail_fragment": "1 mance significantly as shown in Appendix A.",
                "recovered_text": (
                    "1 Compared to V1, fine-tuning boosts its performance significantly "
                    "as shown in Appendix A."
                ),
                "action": "html_recovery_preserve_original_fragments",
                "safe_to_apply": True,
            }
        ]
        document_html = (
            "<p>1 mance significantly as shown in Appendix A.</p>\n"
            "<p>Compared to V1, fine-tuning boosts its perfor-</p>"
        )

        updated, applied = adapter.apply_first_page_footnote_html_recovery(
            document_html,
            diagnostics,
        )

        self.assertEqual(updated, document_html)
        self.assertEqual(applied, [])
        self.assertFalse(diagnostics[0]["safe_to_apply"])
        self.assertEqual(diagnostics[0]["action"], "diagnostic_only_generic_quarantine_preferred")

    def test_formula_number_qc_recovers_spaced_number(self) -> None:
        formulas = [
            {
                "label": "formula",
                "text": r"x = y \quad ( 1 0 )",
                "prov": [{"page_no": 2}],
            }
        ]
        html = '<div><math display="block"><mi>x</mi><mo>=</mo><mi>y</mi></math></div>'

        diagnostics = adapter.formula_number_qc_diagnostics(formulas, html)

        self.assertEqual(len(diagnostics), 1)
        self.assertTrue(diagnostics[0]["safe_to_recover"])
        self.assertEqual(diagnostics[0]["recovered_number"], 10)
        self.assertIn("equation_number_recoverable_from_formula_text", diagnostics[0]["reasons"])

    def test_formula_number_qc_does_not_invent_number_without_source_evidence(self) -> None:
        diagnostics = adapter.formula_number_qc_diagnostics(
            [{"label": "formula", "text": r"x = y", "prov": [{"page_no": 2}]}],
            '<div><math display="block"><mi>x</mi><mo>=</mo><mi>y</mi></math></div>',
        )

        self.assertEqual(diagnostics, [])

    def test_formula_tex_qc_sanitizes_bare_alignment_markers(self) -> None:
        formulas = [
            {
                "label": "formula",
                "text": r"m_i^\ell & = \bigoplus_j m_{ij}^\ell , & ( 1 2 )",
                "prov": [{"page_no": 5}],
            }
        ]

        diagnostics = adapter.formula_tex_qc_diagnostics(formulas)
        display_text, reasons = adapter.sanitize_formula_display_text(
            formulas[0]["text"],
            render_fallback=True,
        )

        self.assertEqual(diagnostics, [])
        self.assertIn("bare_alignment_marker_without_alignment_environment", reasons)
        self.assertNotIn("&", display_text)

    def test_formula_tex_qc_unwraps_single_array_for_display(self) -> None:
        formula = (
            r"\begin{array} { r } { \min _ { G } \max _ { D } V ( D , G ) = "
            r"\mathbb { E } _ { x \sim p _ { d a t a } ( x ) } [ \log D ( x ) ] } "
            r"\end{array} \quad ( 1 )"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(
            formula,
            render_fallback=True,
        )

        self.assertIn("unwrapped_single_formula_array_for_display", reasons)
        self.assertNotIn(r"\begin{array}", display_text)
        self.assertNotIn("unnecessary_single_formula_array", adapter._formula_output_safety_reasons(formula))
        self.assertIn(r"\quad ( 1 )", display_text)

    def test_formula_tex_qc_repairs_unmatched_display_braces(self) -> None:
        formula = (
            r"\min _ { G } \max _ { D } V ( D , G ) = "
            r"\mathbb { E } _ { x \sim p _ { d a t a } ( x ) } "
            r"[ \log D ( x ) ] \quad } \quad ( 1 )"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(
            formula,
            render_fallback=True,
        )

        self.assertIn("repaired_unmatched_display_braces", reasons)
        self.assertNotIn(r"\quad } \quad", display_text)
        latex_ok, latex_reasons = adapter.validate_candidate_latex(display_text)
        self.assertTrue(latex_ok, latex_reasons)

    def test_formula_tex_qc_repairs_pm_bold_variable_ocr_artifact(self) -> None:
        formula = (
            r"D _ { G } ^ { * } ( { \pm b x } ) = "
            r"\frac { p _ { d a t a } ( { \pm b x } ) } "
            r"{ p _ { d a t a } ( { \pm b x } ) + p _ { g } ( { \pm b x } ) }"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(formula)

        self.assertNotIn(
            "repaired_pm_bold_variable_ocr_artifact",
            reasons,
        )
        self.assertIn(r"\pm b x", display_text)
        self.assertNotIn(r"\mathbf { x }", display_text)

    def test_formula_tex_qc_repairs_pm_bold_variable_ocr_artifact_from_route_b(self) -> None:
        formula = (
            r"D _ { G } ^ { * } ( { \pm b x } ) = "
            r"\frac { p _ { d a t a } ( { \pm b x } ) } "
            r"{ p _ { d a t a } ( { \pm b x } ) + p _ { g } ( { \pm b x } ) }"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(
            formula,
            allow_inventive_repairs=True,
        )

        self.assertIn("repaired_pm_bold_variable_ocr_artifact", reasons)
        self.assertNotIn(r"\pm b", display_text)
        self.assertIn(r"\mathbf { x }", display_text)

    def test_formula_tex_qc_preserves_legitimate_pm_b_expression(self) -> None:
        formula = r"y = a \pm b + c"

        display_text, reasons = adapter.sanitize_formula_display_text(formula)

        self.assertNotIn("repaired_pm_bold_variable_ocr_artifact", reasons)
        self.assertEqual(display_text, formula)

    def test_formula_tex_qc_preserves_half_open_interval(self) -> None:
        formula = (
            r"\lim _ { k \to \infty } \rho ^ { - k } "
            r"\left\| x ( k ) - x ^ { * } \right\| = 0, "
            r"\forall \rho \in \left( \gamma , 1 \right] ."
        )

        display_text, reasons = adapter.sanitize_formula_display_text(formula)

        self.assertNotIn("repaired_unmatched_display_parentheses", reasons)
        self.assertEqual(display_text, formula)

    def test_formula_tex_qc_preserves_invisible_right_delimiter(self) -> None:
        formula = (
            r"\left\{ \begin{array}{l} x_i = 1 \\ y_i = 2 "
            r"\end{array} \right. \quad ( 2 )"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(formula)

        self.assertNotIn("downgraded_unbalanced_left_right_commands", reasons)
        self.assertEqual(display_text, formula)
        self.assertNotIn(
            "latex_left_right_mismatch",
            adapter._formula_output_safety_reasons(display_text),
        )

    def test_formula_tex_qc_repairs_log_argument_and_stale_number_artifact(self) -> None:
        formula = (
            r"C ( G ) = - \log + K L \left ( p _ { d a t a } \left \| "
            r"\frac { p _ { d a t a } + p _ { g } } { 2 } \right ) "
            r"+ K L \left ( p _ { g } \left \| "
            r"\frac { p _ { d a t a } + p _ { g } } { 2 } \right ) \right ) "
            r"\quad ( 5 ) \quad ( 4 )"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(
            formula,
            render_fallback=True,
            allow_inventive_repairs=True,
        )

        self.assertIn("repaired_empty_log_argument_from_stale_trailing_number", reasons)
        self.assertIn("downgraded_unbalanced_left_right_commands", reasons)
        self.assertIn(r"\log ( 4 )", display_text)
        self.assertIn(r"\quad ( 5 )", display_text)
        self.assertNotIn(r"\quad ( 4 )", display_text)
        self.assertNotIn(r"\left", display_text)
        self.assertNotIn(r"\right", display_text)
        self.assertNotIn("latex_left_right_mismatch", adapter._formula_output_safety_reasons(display_text))

    def test_formula_tex_qc_does_not_repair_stale_log_trailing_artifact_without_route_b(self) -> None:
        formula = (
            r"C ( G ) = - \log + K L \left ( p _ { d a t a } \left \| "
            r"\frac { p _ { d a t a } + p _ { g } } { 2 } \right ) "
            r"+ K L \left ( p _ { g } \left \| "
            r"\frac { p _ { d a t a } + p _ { g } } { 2 } \right ) \right ) "
            r"\quad ( 5 ) \quad ( 4 )"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(formula)

        self.assertNotIn(
            "repaired_empty_log_argument_from_stale_trailing_number",
            reasons,
        )
        self.assertIn(r"\quad ( 4 )", display_text)
        self.assertIn(r"\log +", display_text)

    def test_formula_tex_qc_removes_balanced_outer_array_group(self) -> None:
        formula = (
            r"{ \begin{array} { r l } & { a = b \quad ( 3 ) } \\ "
            r"& { c = d \quad ( 4 ) } \end{array} }"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(
            formula,
            render_fallback=True,
        )

        self.assertIn("removed_balanced_outer_formula_group", reasons)
        self.assertTrue(display_text.startswith(r"\begin{array}"))
        self.assertNotIn("latex_unmatched_closing_brace", adapter._formula_output_safety_reasons(formula))

    def test_formula_tex_qc_does_not_regress_multiline_array_structure(self) -> None:
        formula = (
            r"\begin{array} { r c l } { \Delta w ^ { t } } & { = } & "
            r"{ p ^ { t } \Delta w ^ { t - 1 } - ( 1 - p ^ { t } ) "
            r"\epsilon ^ { t } \langle \nabla _ { w } L \rangle } \\ "
            r"{ w ^ { t } } & { = } & { w ^ { t - 1 } + \Delta w ^ { t } , } "
            r"\end{array}"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(
            formula,
            render_fallback=True,
        )

        self.assertIn("skipped_array_row_wrapper_repair_regressed_structure", reasons)
        self.assertIn(r"\langle \nabla _ { w } L \rangle } \\", display_text)
        self.assertIn(r"+ \Delta w ^ { t } , } \end{array}", display_text)

    def test_formula_fallback_keeps_nested_cases_array_intact(self) -> None:
        formula = (
            r"\begin{array} { c c l l } { { \epsilon ^ { t } } } & { = } & "
            r"{ { \epsilon _ { 0 } f ^ { t } } } \\ { { p ^ { t } } } & { = } & "
            r"{ { \begin{cases} { \frac { t } { T } p _ { i } + "
            r"( 1 - \frac { t } { T } ) p _ { f } } & { t < T } \\ "
            r"{ p _ { f } } & { t \geq T } \end{cases} } } \end{array}"
        )

        display, source, reasons = adapter._readable_formula_fallback_display_text(
            {"route_a_text": formula}
        )

        self.assertEqual(source, "route_a_source")
        self.assertIn(r"\begin{cases}", display)
        self.assertNotIn("latex_environment_mismatch", reasons)

    def test_formula_fallback_collapses_misdetected_prose_candidate(self) -> None:
        html = adapter._render_formula_fallback_html(
            {
                "formula_no": 1,
                "status": "final_output_unsafe",
                "eq_number": 1,
                "route_a_text": (
                    r"\begin{array}{rl} & { t h e o r d e r o f c o m p u t a t i o n } \\ "
                    r"& \alpha _ { t } = \alpha \cdot \sqrt { 1 - \beta _ { 2 } ^ { t } } \\ "
                    r"& { 2 . 1 } & { A D A M U P D A T E R U L E } \end{array}"
                ),
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_display_fallback"),
        )

        self.assertIn("recognized formula text was withheld", html)
        self.assertNotIn(r"\alpha", html)
        self.assertNotIn("t h e o r d e r", html)
        self.assertNotIn("<details", html)
        self.assertNotIn("second-pass review", html)

    def test_formula_renderer_links_second_pass_review_only_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            sidecar_dir = output_dir / "formula_second_pass"
            sidecar_dir.mkdir()
            (sidecar_dir / "review_index.html").write_text("review", encoding="utf-8")

            html = adapter._render_second_pass_formula_html(
                {
                    "formula_no": 1,
                    "status": "reviewed",
                    "markdown_after": r"$$x = y$$",
                },
                output_dir,
                sidecar_dir,
            )

        self.assertIn('href="formula_second_pass/review_index.html"', html)

    def test_broken_local_refs_audits_final_html_and_markdown_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "present.png").write_bytes(b"png")
            broken = adapter.broken_local_refs(
                output_dir,
                {
                    "html_content": (
                        '<img src="present.png"><a href="missing-review.html#formula-1">'
                        "review</a>"
                    ),
                    "md_content": (
                        "[present](present.png)\n"
                        "![missing](missing-image.png)\n"
                    ),
                },
            )

        self.assertEqual(broken, ["missing-image.png", "missing-review.html"])

    def test_broken_local_refs_ignores_literal_links_in_markdown_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            broken = adapter.broken_local_refs(
                output_dir,
                {
                    "html_content": "",
                    "md_content": (
                        "```markdown\n![literal](fenced-missing.png)\n"
                        '<img src="fenced-html-missing.png">\n```\n'
                        "> ~~~html\n> <img src=\"quoted-missing.png\">\n> ~~~\n"
                        "`![inline](inline-missing.png)`\n"
                        "`<img src=\"inline-html-missing.png\">`\n"
                        "![actual](actual-missing.png)\n"
                    ),
                },
            )

        self.assertEqual(["actual-missing.png"], broken)

    def test_formula_markdown_mutators_preserve_fenced_display_literals(self) -> None:
        for opening, closing in (("```text", "```"), ("~~~text", "~~~")):
            with self.subTest(fence=opening):
                fenced = (
                    f"> {opening}\n> $$dummy \\quad (1)$$\n"
                    "> <!-- formula-final-output-fallback formula=1 reason=literal -->\n"
                    f"> {closing}\n"
                )
                markdown = fenced + "\n$$real \\quad (1)$$\n"

                replaced, changed = adapter._replace_markdown_formula_block(
                    markdown,
                    1,
                    "x=y",
                )
                self.assertTrue(changed)
                self.assertTrue(replaced.startswith(fenced))
                self.assertIn("$$x=y$$", replaced[len(fenced) :])

                with tempfile.TemporaryDirectory() as temp_dir:
                    output_dir = Path(temp_dir)
                    (output_dir / "document.md").write_text(
                        markdown,
                        encoding="utf-8",
                    )
                    patched = adapter._patch_markdown_formula_blocks(
                        output_dir,
                        {1: "x=y"},
                    )
                    patched_markdown = (output_dir / "document.md").read_text(
                        encoding="utf-8"
                    )
                    self.assertEqual([1], patched)
                    self.assertTrue(patched_markdown.startswith(fenced))
                    self.assertIn("$$\nx=y\n$$", patched_markdown[len(fenced) :])

                    (output_dir / "formulas").mkdir()
                    (output_dir / "formulas" / "formula_1.png").write_bytes(b"png")
                    (output_dir / "document.md").write_text(
                        fenced + "\n$$unsafe$$\n",
                        encoding="utf-8",
                    )
                    count = adapter._replace_unsafe_markdown_formula_blocks_with_source_images(
                        output_dir,
                        [1],
                    )
                    unsafe_markdown = (output_dir / "document.md").read_text(
                        encoding="utf-8"
                    )
                    self.assertEqual(1, count)
                    self.assertTrue(unsafe_markdown.startswith(fenced))
                    self.assertIn("formulas/formula_1.png", unsafe_markdown[len(fenced) :])

                fallback_comment = (
                    "<!-- formula-final-output-fallback formula=1 reason=unsafe -->"
                )
                collapsed, count = adapter._collapse_markdown_formula_fallbacks(
                    fenced
                    + "\n$$x=y$$\n"
                    + fallback_comment
                    + "\n"
                )
                self.assertEqual(1, count)
                self.assertTrue(collapsed.startswith(fenced))

    def test_formula_sync_and_validation_ignore_leading_fenced_display_math(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            fenced = "```text\n$$dummy$$\n```\n"
            (output_dir / "document.json").write_text(
                json.dumps({"texts": [{"label": "formula", "text": "old"}]}),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                fenced + "\n$$old$$\n",
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                '<html><body><div class="docling-formula-second-pass" '
                'data-formula-index="1"><div class="docling-formula-render">'
                r"\[x=y\]"
                "</div></div></body></html>",
                encoding="utf-8",
            )
            replacement_log = [
                {
                    "formula_no": 1,
                    "status": "replaced",
                    "display_override": "x=y",
                }
            ]

            sync = adapter.synchronize_formula_contract_outputs(
                output_dir,
                replacement_log,
            )
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertEqual([1], sync["markdown_patched_indexes"])
            self.assertTrue(markdown.startswith(fenced))
            self.assertIn("$$x=y$$", markdown[len(fenced) :])

            with patch.object(adapter, "_formula_output_safety_reasons", return_value=[]):
                validation = adapter.validate_formula_second_pass_html(
                    output_dir,
                    replacement_log,
                )
            self.assertEqual([], validation["markdown_formula_mismatch_indexes"])

    def test_formula_sync_and_validation_keep_placeholder_slots_in_formula_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            replacement_log = [
                {"formula_no": 1, "status": "replaced", "display_override": "x=1"},
                {"formula_no": 2, "status": "replaced", "display_override": "y=2"},
            ]
            (output_dir / "document.json").write_text(
                json.dumps(
                    {
                        "texts": [
                            {"label": "formula", "text": "x=1"},
                            {"label": "formula", "text": "y=2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                '<html><body><div class="docling-formula-second-pass" '
                'data-formula-index="1"><div class="docling-formula-render">'
                r"\[x=1\]"
                '</div></div><div class="docling-formula-second-pass" '
                'data-formula-index="2"><div class="docling-formula-render">'
                r"\[y=2\]"
                "</div></div></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "<!-- formula-not-decoded -->\n\n$$y=2$$\n",
                encoding="utf-8",
            )

            with patch.object(adapter, "_formula_output_safety_reasons", return_value=[]):
                before = adapter.validate_formula_second_pass_html(
                    output_dir,
                    replacement_log,
                )
            self.assertEqual([1], before["markdown_formula_mismatch_indexes"])

            adapter.synchronize_formula_contract_outputs(output_dir, replacement_log)
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertNotIn("formula-not-decoded", markdown)
            self.assertIn("$$x=1$$", markdown)
            self.assertIn("$$y=2$$", markdown)
            with patch.object(adapter, "_formula_output_safety_reasons", return_value=[]):
                after = adapter.validate_formula_second_pass_html(
                    output_dir,
                    replacement_log,
                )
            self.assertEqual([], after["markdown_formula_mismatch_indexes"])

    def test_formula_source_binding_preserves_fenced_literal_anchor_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            _write_visible_test_png(output_dir / "formulas" / "formula_1.png")
            formula = _formula_test_node("x=y")
            fenced = (
                "```markdown\n"
                "$$dummy$$\n"
                "<!-- source-formula-anchor:1 -->\n"
                "<!-- local-ai-lab-formula-evidence:start -->\n"
                "![Formula 1](formulas/formula_1.png)\n"
                "<!-- local-ai-lab-formula-evidence:end -->\n"
                "```\n"
            )
            (output_dir / "document.html").write_text(
                '<html><body><div data-formula-index="1">x=y</div>'
                "<!-- source-formula-anchor:1 --></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                fenced + "\n$$x=y$$\n<!-- source-formula-anchor:1 -->\n",
                encoding="utf-8",
            )

            result = adapter.append_formula_source_renderings(
                output_dir,
                [formula],
                formula_crop_diagnostics=[
                    _formula_test_crop_diagnostic(output_dir, 1, formula)
                ],
            )
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

            self.assertEqual([1], result["markdown_covered_indexes"])
            self.assertEqual([], result["duplicate_markdown_anchor_indexes"])
            self.assertTrue(markdown.startswith(fenced))
            self.assertIn(
                "<!-- source-formula-visual:1 -->",
                markdown[len(fenced) :],
            )

    def test_unsafe_markdown_formula_uses_exact_source_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            (output_dir / "formulas" / "formula_1.png").write_bytes(b"png")
            (output_dir / "document.md").write_text(
                "Before\n\n$$\n"
                r"\begin{array}{r}{i c y o u}\\{r i s k.}\end{array}"
                "\n$$\n\nAfter\n",
                encoding="utf-8",
            )

            count = adapter._replace_unsafe_markdown_formula_blocks_with_source_images(
                output_dir,
                [1],
            )
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(count, 1)
        self.assertNotIn("i c y o u", markdown)
        self.assertIn("formulas/formula_1.png", markdown)
        self.assertIn("Exact formula preserved", markdown)

    def test_all_formula_source_crops_are_appended_to_html_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            for index in (1, 2):
                _write_visible_test_png(
                    output_dir / "formulas" / f"formula_{index}.png",
                )
            (output_dir / "document.html").write_text(
                "<html><body><p>Body</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("Body\n", encoding="utf-8")

            formulas = [_formula_test_node("x=y"), _formula_test_node("y=z", page_no=2)]
            result = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                formula_crop_diagnostics=[
                    _formula_test_crop_diagnostic(output_dir, index, formula)
                    for index, formula in enumerate(formulas, start=1)
                ],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_applied_count"], 2)
        self.assertEqual(result["markdown_applied_count"], 2)
        self.assertEqual(result["html_appendix_count"], 2)
        self.assertIn("docling-formula-source-evidence-appendix", html_text)
        self.assertIn("formulas/formula_2.png", markdown)

    def test_append_formula_source_renderings_links_formula_blocks_without_source_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            for index in (1, 2):
                _write_visible_test_png(
                    output_dir / "formulas" / f"formula_{index}.png"
                )

            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    '<div data-formula-index="1"><details><summary>LaTeX</summary>'
                    '<code>x</code></details></div>'
                    '<div data-formula-index="2"><details><summary>LaTeX</summary>'
                    '<code>y</code></details></div>'
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "正文 $$x$$\n正文 $$y$$\n",
                encoding="utf-8",
            )

            formulas = [_formula_test_node("x"), _formula_test_node("y")]
            result = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                formula_crop_diagnostics=[
                    _formula_test_crop_diagnostic(output_dir, index, formula)
                    for index, formula in enumerate(formulas, start=1)
                ],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_covered_indexes"], [1, 2])
        self.assertEqual(result["markdown_covered_indexes"], [1, 2])
        self.assertEqual(result["html_inline_count"], 2)
        self.assertEqual(result["markdown_inline_count"], 2)
        self.assertEqual(result["html_appendix_count"], 0)
        self.assertEqual(result["markdown_appendix_count"], 0)
        self.assertIn("formulas/formula_1.png", html_text)
        self.assertIn("formulas/formula_2.png", html_text)
        self.assertIn("docling-formula-inline-source", html_text)
        self.assertNotIn('loading="lazy"', html_text)
        self.assertIn("formulas/formula_1.png", markdown)
        self.assertIn("formulas/formula_2.png", markdown)

    def test_append_inline_math_source_renderings_reports_exact_anchor_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "pages").mkdir()
            _write_visible_test_png(
                output_dir / "pages" / "page_1.png",
                (120, 80),
            )
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    "inline 1 <!-- source-inline-math-anchor:inline-math-cjk-1 -->\n"
                    "inline 2 <!-- source-inline-math-anchor:inline-math-cjk-2 -->\n"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    "inline 1 <!-- source-inline-math-anchor:inline-math-cjk-1 -->\n"
                    "inline 2 <!-- source-inline-math-anchor:inline-math-cjk-2 -->\n"
                ),
                encoding="utf-8",
            )

            result = adapter.append_inline_math_source_renderings(
                output_dir,
                {
                    "pages": {
                        "1": {"size": {"width": 100.0, "height": 100.0}},
                    }
                },
                [
                    {
                        "anchor": "inline-math-cjk-1",
                        "page_no": 1,
                        "bbox": {
                            "l": 10.0,
                            "r": 30.0,
                            "t": 70.0,
                            "b": 50.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    },
                    {
                        "anchor": "inline-math-cjk-2",
                        "page_no": 1,
                        "bbox": {
                            "l": 40.0,
                            "r": 60.0,
                            "t": 70.0,
                            "b": 50.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    },
                ],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(
            result["expected_anchors"],
            ["inline-math-cjk-1", "inline-math-cjk-2"],
        )
        self.assertEqual(
            result["html_covered_anchors"],
            ["inline-math-cjk-1", "inline-math-cjk-2"],
        )
        self.assertEqual(
            result["markdown_covered_anchors"],
            ["inline-math-cjk-1", "inline-math-cjk-2"],
        )
        self.assertEqual(result["missing_crop_anchors"], [])
        self.assertIn("docling-inline-math-source", html_text)
        self.assertIn("source-inline-math-anchor:inline-math-cjk-1", html_text)
        self.assertIn("source-inline-math-visual:inline-math-cjk-2", markdown)

    def test_append_formula_source_renderings_keeps_suspicious_context_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            source_path = output_dir / "formulas" / "formula_5.png"
            context_path = output_dir / "formulas" / "formula_5_context.png"
            _write_horizontal_rule_test_png(source_path)
            _write_visible_test_png(context_path, (80, 40), background="yellow")
            (output_dir / "document.html").write_text(
                '<html><body><div data-formula-index="5">$$x$$</div></body></html>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x$$\n<!-- source-formula-anchor:5 -->\n", encoding="utf-8"
            )

            formulas = [
                _formula_test_node("x" if index == 5 else f"unused-{index}")
                for index in range(1, 6)
            ]
            metadata = {
                "formula_crop_diagnostics": [
                    _formula_test_crop_diagnostic(output_dir, 5, formulas[4])
                ],
                "suspicious_formula_diagnostics": [
                    {
                        "index": 5,
                        "reasons": ["bbox_likely_line_or_separator"],
                    }
                ],
            }
            primary = {"counts": {"formulas": 5}}
            status = {"quality_signals": {"primary_surface": primary}}

            result = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                metadata=metadata,
                status=status,
                primary=primary,
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_inline_count"], 0)
        self.assertEqual(result["markdown_inline_count"], 0)
        self.assertEqual(result["html_appendix_count"], 1)
        self.assertEqual(result["markdown_appendix_count"], 1)
        self.assertEqual(result["missing_candidate_indexes"], [5])
        self.assertIn("formulas/formula_5_context.png", html_text)
        self.assertIn("formulas/formula_5_context.png", markdown)

    def test_formula_context_accepts_source_crop_with_glyph_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            source_path = output_dir / "formulas" / "formula_1.png"
            context_path = output_dir / "formulas" / "formula_1_context.png"
            _write_formula_text_test_png(source_path)
            _write_visible_test_png(context_path, (160, 80))
            formula = _formula_test_node("x=y+z")
            (output_dir / "document.html").write_text(
                '<html><body><div class="formula"><details>'
                '<summary>LaTeX</summary><code>x=y+z</code></details></div>'
                '<!-- source-formula-anchor:1 --></body></html>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x=y+z$$\n<!-- source-formula-anchor:1 -->\n",
                encoding="utf-8",
            )
            metadata = {
                "formula_crop_diagnostics": [
                    _formula_test_crop_diagnostic(output_dir, 1, formula)
                ],
                "suspicious_formula_diagnostics": [
                    {
                        "index": 1,
                        "reasons": [
                            "bbox_likely_line_or_separator",
                            "bbox_too_thin_for_complex_formula",
                            "source_crop_likely_too_thin",
                        ],
                    }
                ],
            }

            result = adapter.append_formula_source_renderings(
                output_dir,
                [formula],
                metadata=metadata,
                primary={"counts": {"formulas": 1}},
            )
            source_has_glyph_geometry = adapter._formula_crop_has_glyph_geometry(
                source_path
            )

        self.assertTrue(source_has_glyph_geometry)
        self.assertEqual(result["html_inline_count"], 1)
        self.assertEqual(result["markdown_inline_count"], 1)
        self.assertEqual(result["candidates"][0]["selected"], "context")

    def test_formula_source_crop_is_placed_at_matching_semantic_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "formulas").mkdir()
            _write_visible_test_png(
                output_dir / "formulas" / "formula_2.png"
            )
            (output_dir / "document.html").write_text(
                (
                    "<html><body><div class=\"formula\"><details>"
                    "<summary>LaTeX</summary><code>x=y</code></details></div>"
                    "<!-- source-formula-anchor:2 --></body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\nx=y\n$$\n<!-- source-formula-anchor:2 -->\n",
                encoding="utf-8",
            )

            formulas = [_formula_test_node("a=b"), _formula_test_node("x=y")]
            result = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                formula_crop_diagnostics=[
                    _formula_test_crop_diagnostic(output_dir, 2, formulas[1])
                ],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_inline_count"], 1)
        self.assertEqual(result["markdown_inline_count"], 1)
        self.assertEqual(result["html_appendix_count"], 0)
        self.assertEqual(result["markdown_appendix_count"], 0)
        self.assertIn("docling-formula-inline-source", html_text)
        self.assertIn("formulas/formula_2.png", html_text)
        self.assertIn("formulas/formula_2.png", markdown)
        self.assertIn(
            '<details class="docling-source-disclosure docling-formula-source-disclosure">',
            markdown,
        )
        self.assertNotIn("<details open", markdown)
        self.assertNotIn("Original formula renderings", html_text)

    def test_formula_review_targets_accepts_context_crops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "review"
            output_dir.mkdir()
            (output_dir / "formulas").mkdir()
            (output_dir / "formulas" / "formula_2_context.png").write_bytes(b"png")

            targets = adapter.formula_review_targets(output_dir)

        self.assertEqual(
            targets,
            {2: {"context": "formulas/formula_2_context.png"}},
        )

    def test_formula_source_link_uses_context_crop_when_source_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(
                adapter.formula_source_links(
                    2,
                    {"context": "formulas/formula_2_context.png"},
                ),
                (
                    ' <span class="docling-formula-source" data-formula-index="2">'
                    '<a href="formulas/formula_2_context.png">context crop</a>'
                    "</span>"
                ),
            )

    def test_formula_fallback_markdown_outputs_readable_math_block(self) -> None:
        markdown = adapter._render_formula_fallback_markdown(
            {
                "formula_no": 11,
                "status": "final_output_unsafe",
                "route_a_text": (
                    r"\begin{array}{ll} { p l i c a t i o n } \colon \\ "
                    r"{ \frac { \partial \ell } { \partial x_i } = "
                    r"\frac { \partial \ell } { \partial y_i } \cdot \gamma } \\ "
                    r"{ \frac { \partial \ell } { \partial \mu_B } = 0 } \end{array}"
                ),
            }
        )

        self.assertTrue(markdown.startswith("$$"))
        self.assertIn(r"\partial", markdown)
        self.assertNotIn("Formula 11 fallback", markdown)

    def test_formula_fallback_keeps_raw_log_formula_text_without_route_b(self) -> None:
        html = adapter._render_formula_fallback_html(
            {
                "formula_no": 8,
                "status": "final_output_unsafe",
                "fallback_reason": "latex_left_right_mismatch",
                "route_a_text": (
                    r"C ( G ) = - \log + 2 \cdot J S D \left ( p _ { d a t a } "
                    r"\left \| p _ { g } \right ) \quad ( 6 ) \quad ( 4 )"
                ),
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        self.assertIn("docling-formula-readable-fallback", html)
        self.assertIn('data-formula-status="final_output_unsafe"', html)
        self.assertNotIn(r"\log ( 4 )", html)

    def test_algorithm_array_is_not_rendered_as_formula(self) -> None:
        formula = (
            r"\begin{array}{lll}\text {Input:} & \alpha & \text {stepsize}\\"
            r"\\ \text {Output:} & \theta_t & \text {parameters}\\"
            r"\\ \mathbf{while}\ t < T & \text {do update} & \end{array}"
        )

        html = adapter._render_formula_fallback_html(
            {
                "formula_no": 3,
                "status": "final_output_unsafe",
                "route_b_candidate": formula,
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        self.assertIn("docling-algorithm-block", html)
        self.assertNotIn("docling-formula-render docling-formula-preserved-source", html)
        self.assertIn("Input:", html)

    def test_formula_renderer_preserves_raw_tex_when_display_is_sanitized(self) -> None:
        html = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "qc_formula_tex_safety",
                "markdown_after": r"$$m_i^\ell & = m_{ij}^\ell , & ( 1 2 )$$",
                "display_override": r"m_i^\ell = m_{ij}^\ell , ( 1 2 )",
                "raw_tex": r"m_i^\ell & = m_{ij}^\ell , & ( 1 2 )",
            },
            Path("/tmp/out"),
            Path("/tmp/out"),
        )

        self.assertIn(r"\[m_i^\ell = m_{ij}^\ell , ( 1 2 )\]", html)
        self.assertIn(r"m_i^\ell &amp; = m_{ij}^\ell , &amp; ( 1 2 )", html)
        self.assertIn("docling-formula-display-tex", html)

    def test_cn_polish_replaces_existing_second_pass_formula_block(self) -> None:
        original = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "replaced",
                "markdown_after": r"$$old \quad (12)$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )
        replacement = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "cn_final_polish",
                "markdown_after": r"$$new \quad (12)$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        duplicate = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "replaced",
                "markdown_after": r"$$duplicate \quad (12)$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        updated, changed = adapter._replace_existing_second_pass_formula_block(
            "<html><body>" + original + duplicate + "</body></html>",
            12,
            replacement,
        )

        self.assertTrue(changed)
        self.assertIn(r"new \quad (12)", updated)
        self.assertNotIn(r"old \quad (12)", updated)
        self.assertNotIn(r"duplicate \quad (12)", updated)
        self.assertEqual(updated.count('data-formula-index="12"'), 1)

    def test_cn_formula_sources_reject_unbound_custom_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guarded = root / "guarded"
            sidecar = root / "sidecar"
            guarded.mkdir()
            sidecar.mkdir()
            formulas = [
                {
                    "label": "formula",
                    "text": (
                        rf"x_{{{number}}} = y \quad ( {number} ) trailing"
                        if number == 1
                        else rf"x_{{{number}}} = y \quad ( {number} )"
                    ),
                }
                for number in range(1, 25)
            ]
            (guarded / "document.json").write_text(
                adapter.json.dumps({"texts": formulas}),
                encoding="utf-8",
            )
            replacement_log = [
                {
                    "formula_no": number,
                    "status": "replaced",
                    "route_b_candidate": rf"route_b_{{{number}}}",
                }
                for number in (3, 4, 5, 7, 8, 14, 16)
            ]
            (sidecar / "second_pass_summary.json").write_text(
                adapter.json.dumps({"replacement_log": replacement_log}),
                encoding="utf-8",
            )
            args = Namespace(
                formula_second_pass_guarded_fallback_dir=[
                    f"route-a-full={guarded}"
                ]
            )

            with patch.object(adapter, "_default_cn_route_b_dirs", return_value=[]):
                texts, sources = adapter._cn_accepted_formula_source_texts(args, sidecar)

        self.assertEqual({}, texts)
        self.assertEqual({}, sources)

    def test_cn_formula_sources_reject_wrong_body_and_foreign_equation_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guarded = root / "guarded"
            sidecar = root / "sidecar"
            guarded.mkdir()
            sidecar.mkdir()
            (guarded / "document.json").write_text(
                adapter.json.dumps({
                    "texts": [{
                        "label": "formula",
                        "text": r"WRONG_BODY \\quad ( 2 )",
                    }],
                }),
                encoding="utf-8",
            )
            args = Namespace(
                formula_second_pass_guarded_fallback_dir=[
                    f"route-a-full={guarded}"
                ]
            )

            with patch.object(adapter, "_default_cn_route_b_dirs", return_value=[]):
                texts, sources = adapter._cn_accepted_formula_source_texts(
                    args,
                    sidecar,
                )

        self.assertNotIn(1, texts)
        self.assertNotIn(1, sources)

    def test_cn_formula_sources_accept_reviewed_clean_formula_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guarded = root / "guarded"
            sidecar = root / "sidecar"
            guarded.mkdir()
            sidecar.mkdir()
            (guarded / "document.json").write_text(
                adapter.json.dumps({
                    "texts": [{
                        "label": "formula",
                        "text": (
                            r"c ^ { \prime } _ { p } = O ( c _ { p } ) "
                            r"\times W _ { c }"
                        ),
                    }],
                }),
                encoding="utf-8",
            )
            args = Namespace(
                formula_second_pass_guarded_fallback_dir=[
                    f"route-a-full={guarded}"
                ]
            )

            with patch.object(adapter, "_default_cn_route_b_dirs", return_value=[]):
                texts, sources = adapter._cn_accepted_formula_source_texts(
                    args,
                    sidecar,
                )

        self.assertEqual([1], sorted(texts))
        self.assertEqual("guarded_fallback_full", sources[1])
        self.assertEqual([1], adapter._compact_formula_numbers(texts[1]))

    def test_cn_final_polish_missing_identity_does_not_partially_mutate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            guarded = root / "guarded"
            sidecar = root / "sidecar"
            output_dir.mkdir()
            guarded.mkdir()
            sidecar.mkdir()
            originals = {
                "document.json": adapter.json.dumps({
                    "texts": [{
                        "label": "formula",
                        "text": r"WRONG_BODY \quad ( 2 )",
                    }],
                }),
                "document.md": "original markdown",
                "document.html": "<html><body>original html</body></html>",
            }
            for name, value in originals.items():
                (output_dir / name).write_text(value, encoding="utf-8")
            (guarded / "document.json").write_text(
                originals["document.json"],
                encoding="utf-8",
            )
            args = Namespace(
                input_file=Path("CN.pdf"),
                expected_input_sha256=adapter.CN_ACCEPTED_BASELINE[
                    "source_pdf_sha256"
                ],
                cn_ocr_parity=True,
                legacy_cn_accepted_baseline=True,
                formula_second_pass_guarded_fallback_dir=[
                    f"route-a-full={guarded}"
                ],
            )

            with patch.object(adapter, "_default_cn_route_b_dirs", return_value=[]):
                result = adapter.apply_cn_final_document_polish(
                    output_dir,
                    sidecar,
                    args,
                )

            self.assertFalse(result["ok"])
            self.assertFalse(result["applied"])
            self.assertIn(1, result["missing_source_formulas"])
            for name, value in originals.items():
                self.assertEqual(
                    value,
                    (output_dir / name).read_text(encoding="utf-8"),
                )

    def test_cn_default_sources_keep_first_formulas_clean(self) -> None:
        if not adapter._default_cn_route_b_dirs() or not adapter._default_cn_guarded_fallback_dirs():
            self.skipTest("local CN default formula sources are not available")
        args = Namespace(formula_second_pass_guarded_fallback_dir=[])

        texts, sources = adapter._cn_accepted_formula_source_texts(
            args,
            Path("/tmp/nonexistent-sidecar"),
        )

        self.assertIn(sources[1], {"route_b", "accepted_cn_baseline"})
        self.assertIn(sources[2], {"route_b", "accepted_cn_baseline"})
        self.assertNotIn(r"_ { \, _ { p } }", texts[1])
        self.assertEqual(adapter._compact_formula_numbers(texts[2]), [2])
        self.assertNotIn(r"_ { \, _ { p } }", texts[2])

    def test_formula_final_canonicalization_trims_noise_and_duplicate_number(self) -> None:
        text, repairs = formula_second_pass.canonicalize_formula_output(
            r"x = y \quad ( 9 ) \quad ( 9 ) "
            + " ".join([r"\mathfrak { m }"] * 12),
            9,
        )

        self.assertEqual(text, r"x = y \quad ( 9 )")
        self.assertIn("trimmed_hallucinated_suffix", repairs)
        self.assertEqual(adapter._compact_formula_numbers(text), [9])

    def test_formula_final_canonicalization_removes_low_information_trailing_array(self) -> None:
        text, repairs = formula_second_pass.canonicalize_formula_output(
            r"h' = \operatorname{ReLU}(x) \quad "
            r"\begin{array}{ll}{K_{t-1}}\\{\,}\end{array} ( 17 )",
            17,
        )

        self.assertEqual(text, r"h' = \operatorname{ReLU}(x) \quad ( 17 )")
        self.assertIn("trimmed_low_information_trailing_array", repairs)

    def test_formula_safety_rejects_cjk_and_identical_integral_limits(self) -> None:
        self.assertIn(
            "formula_contains_cjk_prose",
            adapter._formula_output_safety_reasons(r"x = y \quad \text{其中}"),
        )
        self.assertIn(
            "identical_integral_limits",
            adapter._formula_output_safety_reasons(r"x = \int_{t-1}^{t-1} f(t)"),
        )

    def test_formula_normalization_preserves_ambiguous_tokens(self) -> None:
        self.assertEqual(
            formula_second_pass.normalize_formula_candidate(
                r"q' = 0 ( q ) \times W_q"
            ),
            r"q' = 0 ( q ) \times W_q",
        )
        self.assertEqual(
            formula_second_pass.normalize_formula_candidate(
                r"e _ { _ { h \rightarrow p } } = x"
            ),
            r"e _ { _ { h \rightarrow p } } = x",
        )

    def test_formula_safety_rejects_malformed_wrapper_candidates(self) -> None:
        self.assertIn(
            "malformed_nested_subscript",
            adapter._formula_output_safety_reasons(r"c' _ { \, _ { p } } = O(c_p)"),
        )

    def test_formula_safety_does_not_treat_array_columns_as_spaced_prose(self) -> None:
        formula = (
            r"\begin{array}{r l r l r l} & \min_x f(x) \\ "
            r"& \mathrm{subject\,to} \\ & g_i(x) \ge 0 \end{array}"
        )

        self.assertNotIn(
            "garbled_letter_spaced_text",
            adapter._formula_output_safety_reasons(formula),
        )
        self.assertNotIn(
            "unnecessary_single_formula_array",
            adapter._formula_output_safety_reasons(
                r"\begin{array}{r} q' = O(q) \times W \end{array}"
            ),
        )

    def test_cn_html_sequence_completion_inserts_before_next_formula(self) -> None:
        formula_14 = adapter._render_second_pass_formula_html(
            {
                "formula_no": 14,
                "status": "cn_final_polish",
                "markdown_after": "$$fourteen \\quad ( 14 )$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )
        html_text = f"<html><body>{formula_14}</body></html>"

        updated, inserted = adapter._complete_cn_formula_html_sequence(
            html_text,
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
            {
                13: r"thirteen \quad ( 13 )",
                14: r"fourteen \quad ( 14 )",
            },
            {},
        )

        self.assertEqual(inserted, [13])
        self.assertLess(
            updated.index('data-formula-index="13"'),
            updated.index('data-formula-index="14"'),
        )

    def test_apply_all_review_counts_every_formula(self) -> None:
        formulas = [
            {"label": "formula", "text": r"x = y \quad ( 1 0 )", "prov": [{"page_no": 1}]},
            {"label": "formula", "text": r"z = q", "prov": [{"page_no": 1}]},
        ]
        number_diag = [
            {
                "index": 1,
                "safe_to_recover": True,
                "recovered_number": 10,
                "reasons": ["equation_number_recoverable_from_formula_text"],
            },
            {
                "index": 2,
                "safe_to_recover": False,
                "reasons": ["display_formula_missing_equation_number"],
            },
        ]

        review = adapter.formula_second_pass_apply_all_review(formulas, number_diag, [], [1])

        self.assertEqual(review["reviewed_count"], 2)
        self.assertEqual(review["enhanced_count"], 1)
        self.assertEqual(review["evidence_only_count"], 1)

    def test_formula_second_pass_apply_all_replaces_clean_formula(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 700, "b": 680}}],
                }
            ]
        }
        route_b = [
            {
                "text": r"x = y + z \quad (1)",
                "page_no": 1,
                "main_eq": 1,
                "bbox_norm": {"l": 20, "r": 200, "t": 100, "b": 120},
                "node": {},
            }
        ]

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual(log[0]["status"], "replaced")
        self.assertEqual(patched["texts"][0]["text"], r"x = y + z \quad (1)")

    def test_formula_second_pass_keeps_duplicate_equation_matches_anchored(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"a \quad (13)",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 100, "t": 500, "b": 480}}],
                },
                {
                    "label": "formula",
                    "text": r"b \quad (14)",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 100, "t": 450, "b": 430}}],
                },
            ]
        }
        route_b = [
            {
                "text": r"a + c \quad (13)",
                "page_no": 3,
                "main_eq": 13,
                "bbox_norm": {"l": 20, "r": 200, "t": 680, "b": 720},
                "node": {},
            },
            {
                "text": r"b + d \quad (14)",
                "page_no": 3,
                "main_eq": 14,
                "bbox_norm": {"l": 20, "r": 200, "t": 780, "b": 820},
                "node": {},
            },
        ]

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual([entry["status"] for entry in log], ["replaced", "replaced"])
        self.assertEqual(patched["texts"][0]["text"], r"a + c \quad (13)")
        self.assertEqual(patched["texts"][1]["text"], r"b + d \quad (14)")

    def test_formula_matching_does_not_shift_candidate_downstream(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"first \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 760, "b": 740}}],
                },
                {
                    "label": "formula",
                    "text": r"second \quad (2)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 660, "b": 640}}],
                },
            ]
        }
        route_b_doc = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"converted_second \quad (2)",
                    "prov": [{"page_no": 1, "bbox": {"l": 20, "r": 200, "t": 360, "b": 400}}],
                }
            ]
        }
        route_b = formula_second_pass.extract_formulas(route_b_doc)

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertNotEqual(log[0]["status"], "replaced")
        self.assertEqual(log[0]["formula_no"], 1)
        self.assertEqual(log[1]["status"], "replaced")
        self.assertEqual(log[1]["formula_no"], 2)
        self.assertEqual(patched["texts"][0]["text"], r"first \quad (1)")
        self.assertEqual(patched["texts"][1]["text"], r"converted_second \quad (2)")

    def test_formula_markdown_fallback_stays_at_own_anchor(self) -> None:
        markdown = "$$first \\quad (1)$$\n\n$$second \\quad (2)$$"
        entries = [
            {
                "formula_no": 1,
                "anchor_id": "formula-1-page-1-order-0",
                "status": "suspicious_no_route_b_match",
                "fallback_reason": "second_pass_not_applied:no_match",
            },
            {
                "formula_no": 2,
                "anchor_id": "formula-2-page-1-order-1",
                "status": "replaced",
                "route_b_candidate": r"converted_second \quad (2)",
                "eq_number": 2,
            },
        ]

        updated = formula_second_pass.patch_document_md(
            markdown,
            [
                {"text": r"first \quad (1)", "main_eq": 1},
                {"text": r"second \quad (2)", "main_eq": 2},
            ],
            entries,
        )

        self.assertLess(
            updated.index("formula-second-pass-fallback"),
            updated.index("converted_second"),
        )
        self.assertIn("$$first \\quad (1)$$", updated)
        self.assertNotIn("converted_second \\quad (2)$$\n\n$$first", updated)

    def test_failed_latex_keeps_json_formula_and_records_fallback(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 700, "b": 680}}],
                }
            ]
        }
        route_b = formula_second_pass.extract_formulas(
            {
                "texts": [
                    {
                        "label": "formula",
                        "text": r"x = y { \quad (1)",
                        "prov": [{"page_no": 1, "bbox": {"l": 20, "r": 200, "t": 280, "b": 320}}],
                    }
                ]
            }
        )

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual(log[0]["status"], "render_failed_latex")
        self.assertIn("unclosed_brace", log[0]["fallback_reason"])
        self.assertEqual(patched["texts"][0]["text"], r"x = y \quad (1)")
        self.assertEqual(
            patched["texts"][0]["local_ai_lab_formula_second_pass"]["anchor_id"],
            "formula-1-page-1-order-0",
        )

    def test_crop_only_fallback_renders_at_source_anchor(self) -> None:
        original = (
            '<html><body><div><math><annotation>'
            '<span class="docling-formula-source" data-formula-index="1">'
            '<a href="formulas/formula_1_context.png">context crop</a>'
            "</span></annotation></math></div>"
            '<div><math><annotation>'
            '<span class="docling-formula-source" data-formula-index="2"></span>'
            "</annotation></math></div></body></html>"
        )
        entry = {
            "formula_no": 1,
            "anchor_id": "formula-1-page-1-order-0",
            "status": "suspicious_no_route_b_match",
            "fallback_reason": "second_pass_not_applied:no_match",
            "route_b_candidate": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            result = adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["fallback_indexes"], [1])
        self.assertIn('data-formula-index="1"', updated)
        self.assertIn("second_pass_not_applied:no_match", updated)
        self.assertLess(
            updated.index('data-formula-index="1"'),
            updated.index('data-formula-index="2"'),
        )

    def test_missing_html_formula_uses_local_text_neighborhood(self) -> None:
        original = (
            "<html><body>"
            "<p>Paragraph immediately before the omitted formula anchor.</p>"
            "<p>Paragraph immediately after the omitted formula anchor.</p>"
            '<div><math><annotation data-formula-index="2">second</annotation></math></div>'
            "</body></html>"
        )
        entry = {
            "formula_no": 1,
            "anchor_id": "formula-1-page-1-order-0",
            "status": "suspicious_no_route_b_match",
            "fallback_reason": "second_pass_not_applied:no_match",
            "anchor_nearby_before": [
                "Paragraph immediately before the omitted formula anchor."
            ],
            "anchor_nearby_after": [
                "Paragraph immediately after the omitted formula anchor."
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            result = adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        formula_at = updated.index('data-formula-index="1"')
        self.assertLess(updated.index("immediately before"), formula_at)
        self.assertLess(formula_at, updated.index("immediately after"))
        self.assertEqual(
            result["patch_sources"][1],
            "anchor-missing-local-neighborhood-after",
        )

    def test_final_html_replaces_original_mathml_without_duplicate(self) -> None:
        original = (
            "<html><body><p>Before.</p>"
            "<div><math><annotation encoding=\"TeX\">"
            r"x = y \quad ( 1 )"
            "</annotation></math></div><p>After.</p></body></html>"
        )
        entry = {
            "formula_no": 1,
            "status": "replaced",
            "route_a_text": r"x = y \quad ( 1 )",
            "route_b_candidate": "x = y",
            "markdown_after": r"$$x = y \quad ( 1 )$$",
            "eq_number": 1,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            result = adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(updated.count('data-formula-index="1"'), 1)
        self.assertNotIn("<math", updated)
        self.assertIn(r"\quad ( 1 )", updated)

    def test_final_html_recovers_equation_number_from_original_anchor(self) -> None:
        original = (
            "<html><body><div><math><annotation encoding=\"TeX\">"
            r"x = y \quad ( 7 )"
            "</annotation></math></div></body></html>"
        )
        entry = {
            "formula_no": 1,
            "status": "replaced",
            "route_a_text": "x = y",
            "route_b_candidate": "x = y",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertEqual(entry["eq_number"], 7)
        self.assertEqual(entry["equation_number_source"], "original_rendered_anchor")
        self.assertIn(r"\quad ( 7 )", updated)

    def test_equation_numbers_recover_only_inside_bounded_sequence(self) -> None:
        entries = [
            {"formula_no": 1, "eq_number": 1},
            {"formula_no": 2, "eq_number": None},
            {"formula_no": 3, "eq_number": None},
            {"formula_no": 4, "eq_number": 4},
            {"formula_no": 5, "eq_number": None},
        ]

        recovered = adapter._infer_bounded_equation_number_sequence(entries)

        self.assertEqual(recovered, [2, 3])
        self.assertEqual([entry.get("eq_number") for entry in entries], [1, 2, 3, 4, None])
        self.assertEqual(entries[2]["equation_number_source"], "bounded_rendered_sequence")

    def test_final_html_replaces_formula_image_at_same_anchor(self) -> None:
        original = (
            '<html><body><p>Before.</p><figure><img src="formula.png" '
            'alt="q = r (13)" /></figure><p>After.</p></body></html>'
        )
        entry = {
            "formula_no": 13,
            "status": "replaced",
            "route_a_text": "q = r (13)",
            "route_b_candidate": "q = r",
            "markdown_after": r"$$q = r \quad ( 13 )$$",
            "eq_number": 13,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertNotIn("<figure", updated)
        self.assertLess(updated.index("Before."), updated.index('data-formula-index="13"'))
        self.assertLess(updated.index('data-formula-index="13"'), updated.index("After."))

    def test_final_html_gate_rejects_visible_offset_and_image_only_fallback(self) -> None:
        html_text = (
            "<html><head></head><body>"
            '<div class="docling-formula-second-pass docling-formula-fallback" '
            'data-formula-index="2" data-formula-fallback-reason="unsafe"></div>'
            '<div class="docling-formula-second-pass" data-formula-index="1">'
            r'<div class="docling-formula-render">\[x = y\]</div></div>'
            "</body></html>"
        )
        entries = [
            {"formula_no": 1, "status": "replaced", "display_override": "x = y"},
            {
                "formula_no": 2,
                "status": "unsafe",
                "fallback_reason": "unsafe",
                "route_a_text": "bad",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(html_text, encoding="utf-8")
            result = adapter.validate_formula_second_pass_html(output_dir, entries)

        self.assertFalse(result["ok"])
        self.assertTrue(result["visible_offset"])
        self.assertEqual(result["image_only_fallback_indexes"], [2])

    def test_final_html_gate_rejects_garbled_accepted_formula(self) -> None:
        entry = {
            "formula_no": 1,
            "status": "replaced",
            "display_override": "u n k n o w n = x",
        }
        html_text = (
            "<html><head>"
            '<script id="docling-formula-second-pass-mathjax"></script>'
            "</head><body>"
            '<div class="docling-formula-second-pass" data-formula-index="1">'
            r'<div class="docling-formula-render">\[u n k n o w n = x\]</div>'
            "</div></body></html>"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(html_text, encoding="utf-8")
            (output_dir / "document.md").write_text(
                "$$u n k n o w n = x$$",
                encoding="utf-8",
            )
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {"texts": [{"label": "formula", "text": "u n k n o w n = x"}]}
                ),
                encoding="utf-8",
            )
            result = adapter.validate_formula_second_pass_html(output_dir, [entry])

        self.assertFalse(result["ok"])
        self.assertEqual(result["garbled_formula_indexes"], [1])

    def test_formula_alignment_diagnostics_reports_all_second_pass_gaps(self) -> None:
        diagnostics = adapter.formula_second_pass_alignment_diagnostics(
            [
                {
                    "formula_no": 13,
                    "eq_number": 13,
                    "status": "suspicious_no_route_b_match",
                    "route_a_text": "(13)",
                    "route_b_candidate": None,
                    "reasons": ["number_only_missing_body"],
                    "page_no": 4,
                    "route_a_bbox": {"x_center": 700, "y_center": 500},
                },
                {
                    "formula_no": 14,
                    "eq_number": 13,
                    "status": "replaced",
                    "route_a_text": r"bad \quad (13)",
                    "route_b_candidate": r"good \quad (13)",
                    "reasons": ["apply_all_candidate"],
                    "page_no": 4,
                    "route_a_bbox": {"x_center": 700, "y_center": 560},
                },
            ],
            15,
        )

        self.assertFalse(diagnostics["all_formulas_attempted"])
        self.assertIn(15, diagnostics["missing_attempt_indexes"])
        self.assertEqual(diagnostics["sequence_mismatch_count"], 1)
        self.assertEqual(diagnostics["duplicate_equation_number_count"], 1)
        self.assertEqual(diagnostics["missing_body_number_only_count"], 1)
        self.assertEqual(diagnostics["image_formula_not_converted_count"], 1)

    def test_formula_second_pass_apply_all_fallbacks_bad_candidate(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 700, "b": 680}}],
                }
            ]
        }
        route_b = [
            {
                "text": "x = y 中文 \\quad (1)",
                "page_no": 1,
                "main_eq": 1,
                "bbox_norm": {"l": 20, "r": 200, "t": 100, "b": 120},
                "node": {},
            }
        ]

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual(log[0]["status"], "route_b_candidate_failed_quality_gate")
        self.assertEqual(patched["texts"][0]["text"], r"x = y \quad (1)")

    def test_write_formula_latex_sources_outputs_raw_and_display_tex(self) -> None:
        formulas = [
            {
                "label": "formula",
                "text": r"m_i^\ell & = m_{ij}^\ell , & ( 1 2 )",
                "prov": [{"page_no": 5}],
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = adapter.write_formula_latex_sources(Path(tmpdir), formulas)
            text = (Path(tmpdir) / "formulas.tex").read_text()

        self.assertTrue(result["written"])
        self.assertIn("m_i^\\ell & = m_{ij}^\\ell , & ( 1 2 )", text)
        self.assertIn("&", text)

    def test_current_formula_display_fallback_does_not_invent_named_operator(self) -> None:
        document = {
            "texts": [
                {
                    "label": "formula",
                    "text": (
                        r"A t t e n t i o n ( Q , K , V ) = s o f t m a x "
                        r"( \frac { Q K ^ { T } } { \sqrt { d _ { k } } } ) V \quad ( 1 )"
                    ),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                (
                    "<html><body><div><math><mi>A</mi>"
                    '<annotation data-formula-index="1">raw</annotation>'
                    "</math></div></body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    r"$$A t t e n t i o n ( Q , K , V ) = s o f t m a x "
                    r"( \frac { Q K ^ { T } } { \sqrt { d _ { k } } } ) V \quad ( 1 )$$"
                    "\n"
                ),
                encoding="utf-8",
            )
            metadata = {}
            status = {"quality_signals": {}, "warnings": [], "success_class": "degraded_success"}
            args = Namespace(input_file=Path("attention.pdf"))

            result = adapter.apply_current_formula_display_fallback(
                output_dir,
                metadata,
                status,
                args,
                reason="test_no_route_b",
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            md_text = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertIn("docling-formula-second-pass", html_text)
        self.assertNotIn(r"\operatorname{Attention}", html_text)
        self.assertNotIn(r"\operatorname{Attention}", md_text)
        self.assertIn("docling-formula-unavailable", html_text)
        self.assertIn("current_formula_display_fallback", status["quality_signals"])

    def test_current_formula_display_fallback_synchronizes_sanitized_contract_outputs(self) -> None:
        formula = (
            r"C ( G ) = - \log + 2 \cdot J S D \left ( p _ { d a t a } "
            r"\left \| p _ { g } \right ) \quad ( 6 ) \quad ( 4 )"
        )
        document = {"texts": [{"label": "formula", "text": formula}]}
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                (
                    "<html><body><div><math><annotation>"
                    f"{formula}"
                    "</annotation></math></div></body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(f"$${formula}$$", encoding="utf-8")
            metadata: dict[str, object] = {}
            status = {"quality_signals": {}, "warnings": [], "success_class": "degraded_success"}
            args = Namespace(input_file=Path("gan-1406.2661.pdf"))

            result = adapter.apply_current_formula_display_fallback(
                output_dir,
                metadata,  # type: ignore[arg-type]
                status,  # type: ignore[arg-type]
                args,
                reason="test_current_formula_sanitize",
            )
            final_document = adapter.json.loads((output_dir / "document.json").read_text())
            final_markdown = (output_dir / "document.md").read_text()
            final_html = (output_dir / "document.html").read_text()

        self.assertTrue(result["applied"])
        self.assertIn(r"\log +", final_document["texts"][0]["text"])
        self.assertIn(r"\quad ( 4 )", final_document["texts"][0]["text"])
        self.assertIn(r"\log +", final_markdown)
        self.assertIn(r"\quad ( 4 )", final_markdown)
        self.assertIn("docling-formula-unavailable", final_html)

    def test_markdown_main_flow_supplement_adds_html_visible_missing_records(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "4.1 Global Optimality of p_g = p_data",
                    "prov": [{"page_no": 5}],
                },
                {
                    "label": "text",
                    "text": (
                        "Theorem 4.1 The global minimum of the virtual training "
                        "criterion is achieved if and only if p_g = p_data."
                    ),
                    "prov": [{"page_no": 5}],
                },
                {
                    "label": "footnote",
                    "text": "1 This structural note must remain outside main flow.",
                    "prov": [{"page_no": 1}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body><h2>4.1 Global Optimality of p_g = p_data</h2>"
                    "<p>Theorem 4.1 The global minimum of the virtual training "
                    "criterion is achieved if and only if p_g = p_data.</p>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("# Existing\n", encoding="utf-8")
            metadata: dict[str, object] = {}
            status = {"quality_signals": {}, "warnings": []}

            result = adapter.apply_markdown_main_flow_supplement(
                output_dir,
                document,
                metadata,  # type: ignore[arg-type]
                status,  # type: ignore[arg-type]
            )
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertIn(adapter.MARKDOWN_MAIN_FLOW_SUPPLEMENT_START, markdown)
        self.assertIn("4.1 Global Optimality", markdown)
        self.assertIn("Theorem 4.1", markdown)
        self.assertNotIn("structural note", markdown)

    def test_header_footer_qc_flags_page_edge_noise(self) -> None:
        document = {
            "texts": [
                {
                    "label": "page_footer",
                    "text": "2",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {
                                "l": 303,
                                "r": 308,
                                "t": 48,
                                "b": 40,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
                {
                    "label": "page_header",
                    "text": "arXiv:2506.22084v1  [cs.LG]",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 18,
                                "r": 36,
                                "t": 568,
                                "b": 223,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
            ]
        }

        diagnostics = adapter.header_footer_qc_diagnostics(document)

        self.assertEqual(len(diagnostics), 2)
        footer = diagnostics[0]
        header = diagnostics[1]
        self.assertIn("page_number", footer["reasons"])
        self.assertIn("template_or_publication_noise", header["reasons"])
        self.assertIn("rotated_margin_header", header["reasons"])

    def test_structural_quarantine_marks_edge_and_footnote_nodes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "page_footer",
                    "text": "2",
                    "prov": [{"page_no": 2, "bbox": {"l": 303, "r": 308, "t": 48, "b": 40}}],
                },
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [{"page_no": 1, "bbox": {"l": 120, "r": 124, "t": 90, "b": 85}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 2)
        self.assertEqual(qc["unresolved_footnote_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_page_footer")
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote")

    def test_structural_quarantine_marks_plain_text_bottom_footnote_candidate(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Introduction",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 240, "t": 705, "b": 680}}],
                },
                {
                    "label": "text",
                    "text": "1 Correspondence to: author@example.org",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 360, "t": 92, "b": 82}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote_candidate")
        self.assertIn("marker_led_footnote_content_candidate", qc["candidates"][0]["reasons"])

    def test_structural_qc_does_not_treat_edge_section_heading_as_footer(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "2.3 Model Architecture",
                    "prov": [{"page_no": 3, "bbox": {"l": 318, "r": 439, "t": 138, "b": 129}}],
                }
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 0)
        self.assertEqual(document["texts"][0]["label"], "section_header")

    def test_structural_qc_normalizes_strict_top_edge_against_nonzero_page_bottom(self) -> None:
        document = {
            "pages": {
                "1": {"size": {"width": 612, "height": 792}},
                "2": {"size": {"width": 612, "height": 792}},
            },
            "texts": [
                {
                    "label": "text",
                    "text": "This implies that",
                    "prov": [{"page_no": 1, "bbox": {"l": 72, "r": 154, "t": 716, "b": 707}}],
                },
                {
                    "label": "text",
                    "text": "This implies that",
                    "prov": [{"page_no": 2, "bbox": {"l": 72, "r": 154, "t": 716, "b": 707}}],
                },
                {
                    "label": "page_header",
                    "text": "Running paper title",
                    "prov": [{"page_no": 1, "bbox": {"l": 72, "r": 180, "t": 776, "b": 766}}],
                },
                {
                    "label": "page_header",
                    "text": "Running paper title",
                    "prov": [{"page_no": 2, "bbox": {"l": 72, "r": 180, "t": 776, "b": 766}}],
                },
                {
                    "label": "page_footer",
                    "text": "1",
                    "prov": [{"page_no": 1, "bbox": {"l": 303, "r": 309, "t": 79, "b": 70}}],
                },
                {
                    "label": "page_footer",
                    "text": "2",
                    "prov": [{"page_no": 2, "bbox": {"l": 303, "r": 309, "t": 79, "b": 70}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(document["texts"][0]["label"], "text")
        self.assertEqual(document["texts"][1]["label"], "text")
        self.assertNotIn(
            "This implies that",
            {candidate["text"] for candidate in qc["candidates"]},
        )

    def test_structural_qc_quarantines_text_rendered_inside_picture(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 700, "b": 520},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "AUTHORIZATION MD.! _ MP-75",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 200, "r": 300, "t": 670, "b": 650},
                        }
                    ],
                },
                {
                    "label": "caption",
                    "text": "Figure 1: Scanned business documents",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 120, "r": 480, "t": 510, "b": 495},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(qc["candidates"][0]["kind"], "visual_annotation")
        self.assertEqual(qc["candidates"][0]["confidence"], "high")
        self.assertEqual(document["texts"][0]["label"], "quarantined_visual_annotation")
        self.assertEqual(document["texts"][1]["label"], "caption")

    def test_structural_qc_quarantines_small_ocr_text_just_above_picture_bbox(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 700, "b": 520},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "AUTHORIZATION MD.! _ MP-75",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 208, "r": 251, "t": 767, "b": 762},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "A normal paragraph outside the figure annotation zone.",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 490, "b": 450},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        candidate = qc["candidates"][0]
        self.assertEqual(candidate["kind"], "visual_annotation")
        self.assertEqual(
            candidate["picture_overlap"]["region_match"],
            "small_text_in_expanded_picture_annotation_zone",
        )
        self.assertEqual(document["texts"][1]["label"], "text")

    def test_structural_qc_quarantines_table_ocr_spilling_left_and_above_picture(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 74, "r": 276, "t": 718, "b": 643},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "ASCA",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 15, "r": 31, "t": 753, "b": 746},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "(a) Oversegmented structure annotation",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 112, "r": 238, "t": 636, "b": 629},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_visual_annotation")
        self.assertEqual(document["texts"][1]["label"], "text")

    def test_structural_qc_quarantines_duplicate_text_inside_table_bbox(self) -> None:
        document = {
            "tables": [
                {
                    "label": "table",
                    "prov": [
                        {
                            "page_no": 4,
                            "bbox": {"l": 70, "r": 520, "t": 690, "b": 610},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "AP50 AP75",
                    "prov": [
                        {
                            "page_no": 4,
                            "bbox": {"l": 400, "r": 470, "t": 675, "b": 668},
                        }
                    ],
                }
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(qc["candidates"][0]["kind"], "table_visual_annotation")
        self.assertEqual(document["texts"][0]["label"], "quarantined_table_visual_annotation")

    def test_structural_qc_removes_source_disproved_suffix_with_visual_ocr_support(self) -> None:
        legitimate = (
            "We split the dataset randomly into train validation and test sets "
            "at the document level using a standard split. This results in many "
            "tables for training and fewer tables for testing. An example"
        )
        suffix = " sroup Android rohot eel enjoyable embarrassed"
        body_bbox = {
            "l": 308,
            "r": 547,
            "t": 147,
            "b": 79,
            "width": 239,
            "height": 68,
        }
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 58, "r": 280, "t": 719, "b": 569},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": legitimate + suffix,
                    "prov": [{"page_no": 6, "bbox": body_bbox}],
                },
                {
                    "label": "text",
                    "text": "Android robot",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 20, "r": 75, "t": 700, "b": 694},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "Feel enjoyable",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 18, "r": 70, "t": 682, "b": 676},
                        }
                    ],
                },
            ],
        }
        source_line = {
            "text": legitimate,
            "bbox": body_bbox,
            "source": "pdf_text_character_baseline",
        }

        with patch.object(adapter, "_source_page_text_lines", return_value=[source_line]):
            qc = adapter.structural_noise_qc(
                document,
                {"available": True, "pages": {6: {}}},
            )

        suffix_candidate = next(
            item
            for item in qc["candidates"]
            if item["kind"] == "reading_order_table_annotation"
        )
        self.assertEqual(suffix_candidate["match_mode"], "fragment")
        self.assertEqual(suffix_candidate["text"], suffix)
        self.assertGreaterEqual(
            suffix_candidate["source_grounding"]["supporting_visual_token_count"],
            2,
        )
        self.assertEqual(document["texts"][0]["label"], "text")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    f"<p>{legitimate + suffix}</p>"
                    "<p>Android robot</p><p>Feel enjoyable</p>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"{legitimate + suffix}\n\nAndroid robot\n\nFeel enjoyable\n",
                encoding="utf-8",
            )
            with patch.object(
                adapter,
                "_source_page_text_lines",
                return_value=[source_line],
            ):
                result = adapter.apply_structural_quarantine_to_outputs(
                    output_dir,
                    document,
                    {"available": True, "pages": {6: {}}},
                )
            final_html = adapter._visible_html_text(
                adapter._html_without_structural_content(
                    (output_dir / "document.html").read_text(encoding="utf-8")
                )
            )
            final_markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertIn(legitimate, final_html)
        self.assertIn(legitimate, final_markdown)
        self.assertNotIn("sroup Android", final_html)
        self.assertNotIn("sroup Android", final_markdown)

    def test_structural_qc_quarantines_same_page_picture_annotation_shadow(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 100, "r": 500, "t": 500, "b": 250},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "Guernsey",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 200, "r": 250, "t": 400, "b": 390},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "Guernsey",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 600, "r": 650, "t": 400, "b": 390},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 2)
        self.assertEqual(qc["candidates"][0]["kind"], "visual_annotation")
        self.assertEqual(qc["candidates"][1]["kind"], "visual_annotation_shadow")
        self.assertEqual(
            document["texts"][1]["label"],
            "quarantined_visual_annotation_shadow",
        )

    def test_structural_qc_quarantines_abrupt_visual_suffix_without_body(self) -> None:
        body = (
            "This is a long research paragraph describing the method and its "
            "evaluation in ordinary sentence case. " * 8
            + "The model combines textual and visual information for ACUTE TOXICITY IN MICE"
        )
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": body,
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 50, "r": 300, "t": 550, "b": 100},
                        }
                    ],
                }
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        candidate = qc["candidates"][0]
        self.assertEqual(candidate["kind"], "reading_order_annotation")
        self.assertEqual(candidate["match_mode"], "fragment")
        self.assertEqual(candidate["text"], " for ACUTE TOXICITY IN MICE")
        self.assertEqual(document["texts"][0]["label"], "text")

    def test_structural_fragment_quarantine_removes_only_visible_suffix(self) -> None:
        body = "The model combines textual and visual information for ACUTE TOXICITY IN MICE"
        item = {
            "kind": "reading_order_annotation",
            "text": " for ACUTE TOXICITY IN MICE",
            "page_no": 1,
            "reasons": ["abrupt_terminal_uppercase_fragment"],
            "match_mode": "fragment",
        }

        updated, changed = adapter._replace_html_fragment_with_quarantine(
            f"<html><body><p>{body}</p></body></html>",
            item,
        )

        self.assertTrue(changed)
        self.assertIn("<p>The model combines textual and visual information</p>", updated)
        self.assertNotIn("information for ACUTE TOXICITY", adapter._visible_html_text(updated))

    def test_structural_qc_quarantines_duplicate_shadow_of_page_header(self) -> None:
        document = {
            "texts": [
                {
                    "label": "page_header",
                    "text": "23",
                    "prov": [
                        {
                            "page_no": 23,
                            "bbox": {"l": 350, "r": 366, "t": 604, "b": 594},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "23",
                    "prov": [
                        {
                            "page_no": 23,
                            "bbox": {"l": 353, "r": 364, "t": 604, "b": 594},
                        }
                    ],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 2)
        shadow = next(item for item in qc["candidates"] if item["kind"] == "page_header_shadow")
        self.assertEqual(shadow["confidence"], "high")
        self.assertIn(
            "duplicate_text_overlaps_labeled_structural_region",
            shadow["reasons"],
        )
        self.assertEqual(document["texts"][1]["label"], "quarantined_page_header_shadow")

    def test_structural_qc_writes_evidence_sidecar_and_audits_final_output(self) -> None:
        text = "1 Correspondence to: author@example.org"
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": text,
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 360, "t": 92, "b": 82}}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(f"\n{text}\n", encoding="utf-8")
            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            sidecar = adapter.json.loads(
                (output_dir / "structural_regions.json").read_text(encoding="utf-8")
            )
            content = adapter.json.loads(
                (output_dir / "structural_content.json").read_text(encoding="utf-8")
            )
            final_html = (output_dir / "document.html").read_text(encoding="utf-8")
            final_md = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertEqual(sidecar["quarantine_candidate_count"], 1)
        self.assertEqual(sidecar["candidates"][0]["confidence"], "high")
        self.assertEqual(content["record_count"], 1)
        self.assertEqual(content["records"][0]["kind"], "footnote")
        self.assertEqual(result["html_structural_content_count"], 1)
        self.assertEqual(result["markdown_structural_content_count"], 1)
        self.assertIn("Extracted structural and visual notes", final_html)
        self.assertIn("Correspondence to: author@example.org", final_html)
        self.assertIn("## Extracted structural and visual notes", final_md)
        self.assertIn(text, final_md)
        self.assertNotIn(text, adapter._visible_html_text(
            adapter._html_without_structural_content(final_html)
        ))

    def test_structural_content_exports_high_confidence_structural_and_visual_material(self) -> None:
        candidates = [
            {
                "kind": "page_header",
                "text": "Repeated conference header",
                "page_no": 2,
                "reading_order": 1,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
                "evidence_score": 6,
                "reasons": ["repeated_header"],
            },
            {
                "kind": "visual_annotation",
                "text": "Figure OCR noise",
                "page_no": 2,
                "reading_order": 2,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
                "evidence_score": 7,
                "reasons": ["inside_picture"],
            },
            {
                "kind": "page_footer",
                "text": "Uncertain footer",
                "page_no": 2,
                "reading_order": 3,
                "action": "diagnostic_only",
                "confidence": "medium",
                "evidence_score": 2,
                "reasons": ["bottom_zone"],
            },
        ]

        records = adapter._structural_export_records(candidates)

        self.assertEqual([record["kind"] for record in records], ["page_header", "visual_annotation"])
        self.assertEqual(records[0]["text"], "Repeated conference header")
        self.assertEqual(records[1]["text"], "Figure OCR noise")

    def test_structural_content_deduplicates_same_page_shadow(self) -> None:
        candidates = [
            {
                "kind": "page_footer",
                "text": "Proceedings footer",
                "page_no": 3,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
            },
            {
                "kind": "page_footer_shadow",
                "text": "Proceedings footer",
                "page_no": 3,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
            },
        ]

        records = adapter._structural_export_records(candidates)

        self.assertEqual(len(records), 1)

    def test_note_group_reorders_marker_attached_to_continuation_line(self) -> None:
        records = [
            {
                "index": 1,
                "kind": "footnote",
                "text": "Compared to baseline, performance was signifi-",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 124, "r": 500, "t": 89, "b": 70},
            },
            {
                "index": 2,
                "kind": "footnote",
                "text": "1 cantly better in Appendix A.",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 108, "r": 270, "t": 80, "b": 60},
            },
        ]

        groups = adapter._build_structural_note_groups(records)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["marker"], "1")
        self.assertEqual(
            groups[0]["text"],
            "Compared to baseline, performance was significantly better in Appendix A.",
        )
        self.assertEqual(groups[0]["assembly_reason"], "marker_attached_to_continuation_line")

    def test_note_group_uses_source_baselines_to_separate_adjacent_markers(self) -> None:
        characters = []

        def add_line(text: str, x: float, baseline: float) -> None:
            for char in text:
                index = len(characters)
                characters.append(
                    {
                        "index": index,
                        "text": char,
                        "font_name": "Times",
                        "font_weight": 400,
                        "font_size": 6,
                        "bbox": {
                            "l": x,
                            "r": x + 3,
                            "b": baseline,
                            "t": baseline + 6,
                            "width": 3,
                            "height": 6,
                        },
                    }
                )
                x += 3

        add_line("0", 120, 87)
        add_line("Compared to V1, this draft is improved.", 124, 82)
        add_line("1 While GPT-3 performs signifi-", 121, 72)
        add_line("cantly better.", 108, 62)
        records = [
            {
                "index": 1,
                "kind": "footnote",
                "text": "0",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 120, "r": 124, "t": 93, "b": 86, "width": 4, "height": 7},
            },
            {
                "index": 2,
                "kind": "footnote",
                "text": "Compared to V1, this draft is improved. While GPT-3 performs signifi-",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 120, "r": 500, "t": 89, "b": 68, "width": 380, "height": 21},
            },
            {
                "index": 3,
                "kind": "footnote",
                "text": "1 cantly better.",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 108, "r": 270, "t": 78, "b": 60, "width": 162, "height": 18},
            },
        ]
        source = {
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": characters,
                }
            }
        }

        groups = adapter._build_structural_note_groups(records, source)

        self.assertEqual([(item["marker"], item["text"]) for item in groups], [
            ("0", "Compared to V1, this draft is improved."),
            ("1", "While GPT-3 performs significantly better."),
        ])

    def test_note_group_merges_cross_page_two_column_continuation(self) -> None:
        records = [
            {
                "index": 1,
                "kind": "footnote",
                "text": "4 Bidirectional Trans-",
                "page_no": 3,
                "confidence": "high",
                "bbox": {"l": 320, "r": 525, "t": 87, "b": 77},
            },
            {
                "index": 2,
                "kind": "footnote",
                "text": "5 Another note.",
                "page_no": 4,
                "confidence": "high",
                "bbox": {"l": 320, "r": 520, "t": 108, "b": 98},
            },
            {
                "index": 3,
                "kind": "footnote",
                "text": "former continues on the next page.",
                "page_no": 4,
                "confidence": "high",
                "bbox": {"l": 72, "r": 290, "t": 105, "b": 77},
            },
        ]

        groups = adapter._build_structural_note_groups(records)
        note = next(item for item in groups if item.get("marker") == "4")

        self.assertEqual(
            note["text"],
            "Bidirectional Transformer continues on the next page.",
        )
        self.assertEqual(note["continuation_pages"], [4])
        self.assertEqual(len(note["source_bboxes"]), 2)
        self.assertNotIn(
            "former continues on the next page.",
            [item["text"] for item in groups if item.get("marker") is None],
        )

    def test_note_reference_mapping_requires_unique_same_page_marker(self) -> None:
        notes = [
            {"note_id": "note-1", "page_no": 1, "marker": "1"},
            {"note_id": "note-2", "page_no": 2, "marker": "1"},
        ]
        references = [
            {
                "page_no": 1,
                "marker": "1",
                "node_text": "Body text 1",
                "confidence": "high",
            },
            {
                "page_no": 3,
                "marker": "1",
                "node_text": "Unresolved text 1",
                "confidence": "high",
            },
        ]

        mappings, unresolved = adapter._map_note_references(notes, references)

        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["note_id"], "note-1")
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["reason"], "note_marker_not_found")

    def test_symbol_note_references_are_not_grouped_together(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "A * B † C",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 0, "r": 100, "t": 30, "b": 0},
                        }
                    ],
                }
            ]
        }
        source = {
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": [
                        {"index": 0, "text": "A", "font_size": 10, "bbox": {"l": 5, "r": 10, "t": 20, "b": 10}},
                        {"index": 1, "text": "*", "font_size": 6, "bbox": {"l": 11, "r": 14, "t": 22, "b": 16}},
                        {"index": 2, "text": "B", "font_size": 10, "bbox": {"l": 16, "r": 21, "t": 20, "b": 10}},
                        {"index": 3, "text": "†", "font_size": 6, "bbox": {"l": 22, "r": 25, "t": 22, "b": 16}},
                        {"index": 4, "text": "C", "font_size": 10, "bbox": {"l": 27, "r": 32, "t": 20, "b": 10}},
                    ],
                }
            }
        }
        notes = [
            {"page_no": 1, "marker": "*"},
            {"page_no": 1, "marker": "†"},
        ]

        references = adapter._pdf_inline_note_references(document, source, notes)

        self.assertEqual([item["marker"] for item in references], ["*", "†"])

    def test_pdf_note_reference_can_anchor_missing_text_marker_by_geometry(self) -> None:
        document = {
            "texts": [
                {
                    "label": "title",
                    "text": "Graph neural retrieval",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 10, "r": 110, "t": 28, "b": 10},
                        }
                    ],
                }
            ]
        }
        source = {
            "available": True,
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": [
                        {"index": 0, "text": "l", "font_size": 10, "bbox": {"l": 102, "r": 106, "t": 22, "b": 12}},
                        {"index": 1, "text": "1", "font_size": 6, "bbox": {"l": 112, "r": 115, "t": 28, "b": 22}},
                    ],
                }
            },
        }
        notes = [{"page_no": 1, "marker": "1", "note_id": "docling-note-p1-1-1"}]

        references = adapter._pdf_inline_note_references(document, source, notes)
        mappings, unresolved = adapter._map_note_references(notes, references)
        html_text, html_count = adapter._link_note_references_in_html(
            "<h1>Graph neural retrieval</h1>",
            mappings,
        )
        markdown, markdown_count = adapter._link_note_references_in_markdown(
            "# Graph neural retrieval\n",
            mappings,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["anchor_mode"], "append_missing_marker")
        self.assertEqual(len(unresolved), 0)
        self.assertEqual(html_count, 1)
        self.assertIn('href="#docling-note-p1-1-1"', html_text)
        self.assertEqual(markdown_count, 1)
        self.assertIn('href="#docling-note-p1-1-1"', markdown)

    def test_first_page_publication_note_fallback_links_unique_code_note_to_title(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "Retrieving Complex Tables",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 500, "t": 710, "b": 675}}],
                },
                {
                    "label": "text",
                    "text": "Author One, Author Two",
                    "prov": [{"page_no": 1, "bbox": {"l": 150, "r": 450, "t": 665, "b": 645}}],
                },
                {
                    "label": "section_header",
                    "text": "ABSTRACT",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 120, "t": 600, "b": 590}}],
                },
            ]
        }
        notes = [
            {
                "page_no": 1,
                "marker": "1",
                "note_id": "docling-note-p1-1-1",
                "text": "Code and data are available at https://example.org/repo",
            }
        ]

        references = adapter._first_page_publication_note_references(document, notes, [])
        mappings, unresolved = adapter._map_note_references(notes, references)
        html_text, count = adapter._link_note_references_in_html(
            "<h1>Retrieving Complex Tables</h1><p>Author One, Author Two</p><h2>ABSTRACT</h2>",
            mappings,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["source"], "first_page_publication_note_fallback")
        self.assertEqual(references[0]["node_text"], "Retrieving Complex Tables")
        self.assertEqual(len(unresolved), 0)
        self.assertEqual(count, 1)
        self.assertIn('href="#docling-note-p1-1-1"', html_text)

    def test_first_page_publication_note_fallback_does_not_guess_generic_notes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "Paper Title",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 500, "t": 710, "b": 675}}],
                }
            ]
        }
        notes = [
            {
                "page_no": 1,
                "marker": "1",
                "note_id": "docling-note-p1-1-1",
                "text": "These authors contributed equally.",
            }
        ]

        references = adapter._first_page_publication_note_references(document, notes, [])

        self.assertEqual(references, [])

    def test_note_reference_links_html_and_markdown_with_backlink_ids(self) -> None:
        mappings = [
            {
                "page_no": 1,
                "marker": "1",
                "node_text": "Body text 1",
                "note_id": "docling-note-p1-1-1",
                "reference_id": "docling-note-p1-1-1-ref-1",
            },
            {
                "page_no": 1,
                "marker": "2",
                "node_text": "Body text 1 and 2",
                "note_id": "docling-note-p1-2-1",
                "reference_id": "docling-note-p1-2-1-ref-1",
            },
            {
                "page_no": 1,
                "marker": "1",
                "node_text": "Body text 1 and 2",
                "note_id": "docling-note-p1-1-1",
                "reference_id": "docling-note-p1-1-1-ref-2",
            },
            {
                "page_no": 2,
                "marker": "3",
                "node_text": "Research & development 3",
                "note_id": "docling-note-p2-3-1",
                "reference_id": "docling-note-p2-3-1-ref-1",
            },
        ]

        html_text, html_count = adapter._link_note_references_in_html(
            "<html><body><p>Body text 1</p><p>Body text 1 and 2</p></body></html>",
            mappings,
        )
        markdown, markdown_count = adapter._link_note_references_in_markdown(
            "Body text 1\n\nBody text 1 and 2\n\nResearch &amp; development 3\n",
            mappings,
        )

        self.assertEqual(html_count, 3)
        self.assertIn('href="#docling-note-p1-1-1"', html_text)
        self.assertIn('id="docling-note-p1-1-1-ref-1"', html_text)
        self.assertEqual(markdown_count, 4)
        self.assertIn('href="#docling-note-p1-1-1"', markdown)
        self.assertIn('href="#docling-note-p2-3-1"', markdown)

    def test_html_note_links_match_heading_and_inline_emphasis(self) -> None:
        mappings = [
            {
                "page_no": 1,
                "marker": "*",
                "node_text": "Author Name *",
                "note_id": "docling-note-p1-star-1",
                "reference_id": "docling-note-p1-star-1-ref-1",
            },
            {
                "page_no": 2,
                "marker": "3",
                "node_text": "Use BERTBASE. 3",
                "note_id": "docling-note-p2-3-1",
                "reference_id": "docling-note-p2-3-1-ref-1",
            },
        ]

        html_text, count = adapter._link_note_references_in_html(
            "<h2>Author Name *</h2><p>Use <strong>BERT</strong>BASE. 3</p>",
            mappings,
        )

        self.assertEqual(count, 2)
        self.assertIn('id="docling-note-p1-star-1-ref-1"', html_text)
        self.assertIn('id="docling-note-p2-3-1-ref-1"', html_text)
        self.assertIn("<strong>BERT</strong>BASE", html_text)

    def test_markdown_note_links_ignore_semantic_emphasis_markers(self) -> None:
        mappings = [
            {
                "page_no": 1,
                "marker": "*",
                "node_text": "Yelong Shen * Shean Wang *",
                "note_id": "docling-note-p1-star-1",
                "reference_id": "docling-note-p1-star-1-ref-1",
            },
            {
                "page_no": 1,
                "marker": "*",
                "node_text": "Yelong Shen * Shean Wang *",
                "note_id": "docling-note-p1-star-1",
                "reference_id": "docling-note-p1-star-1-ref-2",
            },
        ]

        markdown, count = adapter._link_note_references_in_markdown(
            "**Yelong Shen** * **Shean Wang** *\n",
            mappings,
        )

        self.assertEqual(count, 2)
        self.assertEqual(markdown.count('href="#docling-note-p1-star-1"'), 2)
        self.assertIn("**Yelong Shen**", markdown)
        self.assertIn("**Shean Wang**", markdown)

    def test_markdown_note_link_matches_ordered_list_visible_text(self) -> None:
        mapping = {
            "page_no": 3,
            "marker": "1",
            "node_text": "The codebase is available at GitHub. 1",
            "note_id": "docling-note-p3-1-1",
            "reference_id": "docling-note-p3-1-1-ref-1",
        }

        markdown, count = adapter._link_note_references_in_markdown(
            "3. Prior contribution.\n4. The codebase is available at GitHub. 1\n",
            [mapping],
        )

        self.assertEqual(count, 1)
        self.assertIn('id="docling-note-p3-1-1-ref-1"', markdown)
        self.assertIn("4. The codebase", markdown)

    def test_bibliography_links_merge_cross_page_continuation_without_offset(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Prior work [1] and related systems [2,3] are compared.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 500, "t": 500, "b": 470}}],
                },
                {
                    "label": "text",
                    "text": "An unresolved citation [4] remains visible.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 500, "t": 460, "b": 430}}],
                },
                {
                    "label": "section_header",
                    "text": "References",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 200, "t": 700, "b": 680}}],
                },
                {
                    "label": "list_item",
                    "text": "Alpha, A.: First reference. Journal (2020) 1",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 650, "b": 620}}],
                },
                {
                    "label": "list_item",
                    "text": "Beta, B.: A reference split across pages. In: Proceedings of the",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 100, "b": 70}}],
                },
                {
                    "label": "list_item",
                    "text": "23rd Conference. pp. 10-20 (2021) 1",
                    "prov": [{"page_no": 3, "bbox": {"l": 50, "r": 500, "t": 700, "b": 670}}],
                },
                {
                    "label": "list_item",
                    "text": "Gamma, C.: Third reference. Journal (2022) 1",
                    "prov": [{"page_no": 3, "bbox": {"l": 50, "r": 500, "t": 650, "b": 620}}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)

        self.assertTrue(diagnostics["available"])
        self.assertEqual(diagnostics["reference_count"], 3)
        self.assertEqual(diagnostics["references"][1]["continuation_count"], 1)
        self.assertEqual(diagnostics["references"][2]["number"], 3)
        self.assertEqual(diagnostics["citation_count"], 2)
        self.assertEqual(diagnostics["linked_number_count"], 3)
        self.assertEqual(diagnostics["unresolved_citation_count"], 1)
        self.assertEqual(
            diagnostics["unresolved_citations"][0]["missing_reference_numbers"],
            [4],
        )
        self.assertEqual(
            document["texts"][5]["local_ai_lab_qc"]["bibliography_reference"]["role"],
            "cross_page_continuation",
        )

    def test_bibliography_links_html_markdown_and_backlinks(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Compare [1,2].",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 200, "t": 500, "b": 480}}],
                },
                {
                    "label": "section_header",
                    "text": "References",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 200, "t": 700, "b": 680}}],
                },
                {
                    "label": "list_item",
                    "text": "Alpha, A.: First reference. (2020) 1",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 650, "b": 620}}],
                },
                {
                    "label": "list_item",
                    "text": "Beta, B.: Second reference. (2021) 1",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 610, "b": 580}}],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, html_references, html_citations = adapter._link_bibliography_in_html(
            (
                "<html><head></head><body><p>Compare [1,2].</p>"
                "<h2>References</h2><ul>"
                "<li>Alpha, A.: First reference. (2020) 1</li>"
                "<li>Beta, B.: Second reference. (2021) 1</li>"
                "</ul></body></html>"
            ),
            diagnostics,
        )
        markdown, md_references, md_citations = adapter._link_bibliography_in_markdown(
            (
                "Compare [1,2].\n\n## References\n\n"
                "- Alpha, A.: First reference. (2020) 1\n"
                "- Beta, B.: Second reference. (2021) 1\n"
            ),
            diagnostics,
        )

        self.assertEqual((html_references, html_citations), (2, 1))
        self.assertIn('href="#docling-reference-1"', html_text)
        self.assertIn('href="#docling-reference-2"', html_text)
        self.assertIn('id="docling-reference-1"', html_text)
        self.assertIn('href="#docling-citation-1-1"', html_text)
        self.assertEqual((md_references, md_citations), (2, 1))
        self.assertIn('href="#docling-reference-1"', markdown)
        self.assertIn('2. <a id="docling-reference-2"></a>', markdown)
        self.assertIn('href="#docling-citation-2-1"', markdown)

    def test_bibliography_links_cross_page_editor_continuation_and_table_cells(self) -> None:
        document = {
            "texts": [
                {
                    "label": "list_item",
                    "text": "Baseline [1]",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "section_header",
                    "text": "References",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "list_item",
                    "text": "Alpha, A.: First reference. (2020)",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "list_item",
                    "text": "Yim, M.: A reference ending with editors",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "list_item",
                    "text": "(eds.) Document Analysis and Recognition. (2021)",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Zhang, Z.: Final reference. (2022)",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "table_cell",
                    "text": "Model [2,3]",
                    "prov": [],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)

        self.assertEqual(diagnostics["reference_count"], 3)
        self.assertEqual(diagnostics["references"][1]["source_list_indexes"], [1, 2])
        self.assertEqual(diagnostics["references"][2]["number"], 3)

        html_text, html_references, html_citations = adapter._link_bibliography_in_html(
            (
                "<h2>References</h2><ol>"
                "<li>Alpha, A.: First reference. (2020)</li>"
                "<li>Yim, M.: A reference ending with editors</li>"
                "<li>(eds.) Document Analysis and Recognition. (2021)</li>"
                "<li>Zhang, Z.: Final reference. (2022)</li></ol>"
                "<ul><li>Baseline [1]</li></ul>"
                "<table><tr><th>Model [2,3]</th></tr></table>"
            ),
            diagnostics,
        )
        markdown, md_references, md_citations = adapter._link_bibliography_in_markdown(
            (
                "## References\n\n"
                "1. Alpha, A.: First reference. (2020)\n"
                "2. Yim, M.: A reference ending with editors\n"
                "- (eds.) Document Analysis and Recognition. (2021)\n"
                "3. Zhang, Z.: Final reference. (2022)\n\n"
                "- Baseline [1]\n\n"
                "| Model [2,3] |\n| --- |\n"
            ),
            diagnostics,
        )

        self.assertEqual((html_references, html_citations), (3, 2))
        self.assertIn('id="docling-reference-3"', html_text)
        self.assertIn('href="#docling-reference-1"', html_text)
        self.assertIn('href="#docling-reference-2"', html_text)
        self.assertIn('href="#docling-reference-3"', html_text)
        self.assertEqual((md_references, md_citations), (3, 2))
        self.assertIn('href="#docling-reference-1"', markdown)
        self.assertIn('href="#docling-reference-2"', markdown)
        self.assertIn('href="#docling-reference-3"', markdown)

    def test_bibliography_preserves_explicit_marker_order_without_double_number(self) -> None:
        document = {
            "texts": [
                {"label": "text", "text": "See [2].", "prov": [{"page_no": 1}]},
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "First",
                    "marker": "[1]",
                    "enumerated": True,
                    "orig": "[1] First",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Third",
                    "marker": "[3]",
                    "enumerated": True,
                    "orig": "[3] Third",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Second",
                    "marker": "[2]",
                    "enumerated": True,
                    "orig": "[2] Second",
                    "prov": [{"page_no": 2}],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>See [2].</p><h2>References</h2><ol>"
                "<li style=\"list-style-type: '[1] ';\">First</li>"
                "<li style=\"list-style-type: '[3] ';\">Third</li>"
                "<li style=\"list-style-type: '[2] ';\">Second</li></ol>"
            ),
            diagnostics,
        )

        self.assertEqual([item["number"] for item in diagnostics["references"]], [1, 3, 2])
        self.assertEqual((references, citations), (3, 1))
        self.assertNotIn('<span class="docling-reference-number">', html_text)
        self.assertIn('id="docling-reference-2">Second', html_text)
        self.assertIn('href="#docling-reference-2"', html_text)

    def test_cn_bibliography_repairs_mixed_and_missing_citation_brackets(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "DKVMN（8］ 模型以及后续工作〔10 提出了改进。",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "参考文献：", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "［8］ Zhang et al. Dynamic memory.",
                    "orig": "［8］ Zhang et al. Dynamic memory.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "［10］ Zong et al. Mastery speed.",
                    "orig": "［10］ Zong et al. Mastery speed.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>DKVMN（8］ 模型以及后续工作〔10 提出了改进。</p>"
                "<h2>参考文献：</h2><ul>"
                "<li>［8］ Zhang et al. Dynamic memory.</li>"
                "<li>［10］ Zong et al. Mastery speed.</li></ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["reference_count"], 2)
        self.assertEqual((references, citations), (2, 2))
        self.assertNotIn("（8］", html_text)
        self.assertNotIn("〔10 ", html_text)
        self.assertNotIn('<span class="docling-reference-number">', html_text)
        self.assertIn('href="#docling-reference-8">8</a>', html_text)
        self.assertIn('href="#docling-reference-10">10</a>', html_text)

    def test_bibliography_links_general_bracket_numeric_ranges(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "相关研究[1~3]覆盖早期方法，增量实验［1～3］补充说明，DKVMN（8）模型随后出现。"
                        "引言综述依托智慧教育平台1~3］。"
                        "其中 i∈[1,t] 且 h∈［1,N］，O'=10[11] 不是文献引用。"
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "参考文献：", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［1］ First range reference.", "orig": "［1］ First range reference.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［2］ Middle range reference.", "orig": "［2］ Middle range reference.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［3］ Last range reference.", "orig": "［3］ Last range reference.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［8］ DKVMN reference.", "orig": "［8］ DKVMN reference.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>相关研究[1~3]覆盖早期方法，增量实验［1～3］补充说明，DKVMN（8）模型随后出现。"
                "引言综述依托智慧教育平台1~3］。"
                "其中 i∈[1,t] 且 h∈［1,N］，O'=10[11] 不是文献引用。</p>"
                "<h2>参考文献：</h2><ul>"
                "<li>［1］ First range reference.</li>"
                "<li>［2］ Middle range reference.</li>"
                "<li>［3］ Last range reference.</li>"
                "<li>［8］ DKVMN reference.</li>"
                "</ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 4)
        self.assertEqual((references, citations), (4, 4))
        self.assertIn('href="#docling-reference-1">1</a>~<a', html_text)
        self.assertIn('href="#docling-reference-2" aria-label="Reference 2"></a>', html_text)
        self.assertIn('href="#docling-reference-3">3</a>', html_text)
        self.assertIn('href="#docling-reference-8">8</a>', html_text)
        self.assertIn("ocr_missing_open_citation_bracket", diagnostics["citations"][3]["mapping_evidence"])
        self.assertIn("i∈[1,t]", html_text)
        self.assertIn("h∈［1,N］", html_text)
        self.assertIn("O'=10[11]", html_text)
        self.assertIn("general_bracket_numeric_citation", diagnostics["citations"][0]["mapping_evidence"])

    def test_cn_bibliography_links_ocr_malformed_author_and_model_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "1.2 时间相关表示",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "text",
                    "text": (
                        "TCN-KT［\"！ 模型融合了基础信息。CKT！12模型建模历史知识点。"
                        "MAFKT! 3］模型描述多尺度关系。李浩君等人「51使用双向GRU。"
                    ),
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "text",
                    "text": "GKT 10 模型利用图结构。Tong等人］利用空间关系。郑浩东等人【20使用知识图。",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "参考文献：", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［11］ 王璨，刘朝晖，等.TCN-KT：个人基础与遗忘融合的时间卷积知识追踪模型［J］.", "orig": "［11］ 王璨，刘朝晖，等.TCN-KT：个人基础与遗忘融合的时间卷积知识追踪模型［J］.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［12］ Shen Shuanghong. Convolutional knowledge tracing.", "orig": "［12］ Shen Shuanghong. Convolutional knowledge tracing.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［13］ 段建设，等. MAFKT：多尺度注意力融合知识追踪模型.", "orig": "［13］ 段建设，等. MAFKT：多尺度注意力融合知识追踪模型.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［15］ 李浩君，方璇，戴海容. 基于自注意力机制和双向 GRU 神经网络的深度知识追踪优化模型.", "orig": "［15］ 李浩君，方璇，戴海容. 基于自注意力机制和双向 GRU 神经网络的深度知识追踪优化模型.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［16］ Nakagawa. Graph-based knowledge tracing.", "orig": "［16］ Nakagawa. Graph-based knowledge tracing.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［19］ Tong Shiwei. Structure-based knowledge tracing.", "orig": "［19］ Tong Shiwei. Structure-based knowledge tracing.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［20］ 郑浩东，马华，谢颖超，等. 融合遗忘因素与记忆门的图神经网络知识追踪模型.", "orig": "［20］ 郑浩东，马华，谢颖超，等. 融合遗忘因素与记忆门的图神经网络知识追踪模型.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, _references, citations = adapter._link_bibliography_in_html(
            (
                "<h2>1.2 时间相关表示</h2>"
                "<p>TCN-KT［\"！ 模型融合了基础信息。CKT！12模型建模历史知识点。"
                "MAFKT! 3］模型描述多尺度关系。李浩君等人「51使用双向GRU。</p>"
                "<p>GKT 10 模型利用图结构。Tong等人］利用空间关系。郑浩东等人【20使用知识图。</p>"
                "<h2>参考文献：</h2><ul>"
                "<li>［11］ 王璨，刘朝晖，等.TCN-KT：个人基础与遗忘融合的时间卷积知识追踪模型［J］.</li>"
                "<li>［12］ Shen Shuanghong. Convolutional knowledge tracing.</li>"
                "<li>［13］ 段建设，等. MAFKT：多尺度注意力融合知识追踪模型.</li>"
                "<li>［15］ 李浩君，方璇，戴海容. 基于自注意力机制和双向 GRU 神经网络的深度知识追踪优化模型.</li>"
                "<li>［16］ Nakagawa. Graph-based knowledge tracing.</li>"
                "<li>［19］ Tong Shiwei. Structure-based knowledge tracing.</li>"
                "<li>［20］ 郑浩东，马华，谢颖超，等. 融合遗忘因素与记忆门的图神经网络知识追踪模型.</li>"
                "</ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 7)
        self.assertEqual(citations, 7)
        for number in [11, 12, 13, 15, 16, 19, 20]:
            self.assertIn(f'href="#docling-reference-{number}">{number}</a>', html_text)
        self.assertIn("1.2 时间相关表示", html_text)
        self.assertNotIn('href="#docling-reference-1">1</a>.2', html_text)

    def test_bibliography_links_author_year_citations_without_malformed_digit_split(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Representations are discussed in [Olah, 2014]. "
                        "Sequence models are trained as in [Graves, 2013], "
                        "then attention follows [Bahdanau et al., 2015]."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "D. Bahdanau, K. Cho, and Y. Bengio. Neural machine translation. In ICLR, 2015.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "A. Graves. Generating sequences with recurrent neural networks. arXiv:1308.0850, 2013.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "C. Olah. Deep learning, NLP, and representations. Blog, 2014.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Representations are discussed in [Olah, 2014]. "
                "Sequence models are trained as in [Graves, 2013], "
                "then attention follows [Bahdanau et al., 2015].</p>"
                "<h2>References</h2><ol>"
                "<li>D. Bahdanau, K. Cho, and Y. Bengio. Neural machine translation. In ICLR, 2015.</li>"
                "<li>A. Graves. Generating sequences with recurrent neural networks. arXiv:1308.0850, 2013.</li>"
                "<li>C. Olah. Deep learning, NLP, and representations. Blog, 2014.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 3)
        self.assertEqual((references, citations), (3, 3))
        self.assertIn('href="#docling-reference-3">Olah, 2014</a>', html_text)
        self.assertIn('href="#docling-reference-2">Graves, 2013</a>', html_text)
        self.assertIn('href="#docling-reference-1">Bahdanau et al., 2015</a>', html_text)
        self.assertNotIn("[1]ah, 2014", html_text)

    def test_bibliography_links_parenthetical_and_narrative_author_year_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Pre-training helps (Dai and Le, 2015; Peters et al., 2018a; "
                        "Radford et al., 2018; Howard and Ruder, 2018). "
                        "Paraphrase results follow (Dolan and Brockett, 2005). "
                        "Unlike Radford et al. (2018), this is bidirectional; "
                        "Peters et al. (2018a) remains feature-based."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Andrew M Dai and Quoc V Le. 2015. Semi-supervised sequence learning.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "William B Dolan and Chris Brockett. 2005. Automatically constructing a corpus.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Jeremy Howard and Sebastian Ruder. 2018. Universal language model fine-tuning.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018a. Deep contextualized word representations.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018b. Dissecting contextual word embeddings.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Alec Radford, Karthik Narasimhan, Tim Salimans, and Ilya Sutskever. 2018. Improving language understanding.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Pre-training helps (Dai and Le, 2015; Peters et al., 2018a; "
                "Radford et al., 2018; Howard and Ruder, 2018). "
                "Paraphrase results follow (Dolan and Brockett, 2005). "
                "Unlike Radford et al. (2018), this is bidirectional; "
                "Peters et al. (2018a) remains feature-based.</p>"
                "<h2>References</h2><ol>"
                "<li>Andrew M Dai and Quoc V Le. 2015. Semi-supervised sequence learning.</li>"
                "<li>William B Dolan and Chris Brockett. 2005. Automatically constructing a corpus.</li>"
                "<li>Jeremy Howard and Sebastian Ruder. 2018. Universal language model fine-tuning.</li>"
                "<li>Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018a. Deep contextualized word representations.</li>"
                "<li>Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018b. Dissecting contextual word embeddings.</li>"
                "<li>Alec Radford, Karthik Narasimhan, Tim Salimans, and Ilya Sutskever. 2018. Improving language understanding.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 4)
        self.assertEqual(diagnostics["linked_number_count"], 7)
        self.assertEqual((references, citations), (6, 4))
        self.assertIn('href="#docling-reference-1">Dai and Le, 2015</a>', html_text)
        self.assertIn('href="#docling-reference-4">Peters et al., 2018a</a>', html_text)
        self.assertIn('href="#docling-reference-6">Radford et al., 2018</a>', html_text)
        self.assertIn('href="#docling-reference-3">Howard and Ruder, 2018</a>', html_text)
        self.assertIn('href="#docling-reference-2">Dolan and Brockett, 2005</a>', html_text)
        self.assertIn('href="#docling-reference-6">Radford et al. (2018)</a>', html_text)
        self.assertIn('href="#docling-reference-4">Peters et al. (2018a)</a>', html_text)

    def test_bibliography_links_escaped_ampersand_author_year_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Deep models are used in vision and speech "
                        "(Deng et al., 2013; Krizhevsky et al., 2012; "
                        "Hinton & Salakhutdinov, 2006; Hinton et al., 2012a; "
                        "Graves et al., 2013). RMSProp follows "
                        "(Tieleman & Hinton, 2012)."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Deng et al. 2013. ImageNet large scale visual recognition.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Alex Krizhevsky et al. 2012. ImageNet classification.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "G. Hinton and R. Salakhutdinov. 2006. Reducing data dimensionality.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Hinton et al. 2012a. Deep neural networks for acoustic modeling.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Graves et al. 2013. Speech recognition with deep recurrent neural networks.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Tieleman and Hinton. 2012. Lecture 6.5 RMSProp.", "prov": [{"page_no": 2}]},
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Deep models are used in vision and speech "
                "(Deng et al., 2013; Krizhevsky et al., 2012; "
                "Hinton &amp; Salakhutdinov, 2006; Hinton et al., 2012a; "
                "Graves et al., 2013). RMSProp follows "
                "(Tieleman &amp; Hinton, 2012).</p>"
                "<h2>References</h2><ol>"
                "<li>Deng et al. 2013. ImageNet large scale visual recognition.</li>"
                "<li>Alex Krizhevsky et al. 2012. ImageNet classification.</li>"
                "<li>G. Hinton and R. Salakhutdinov. 2006. Reducing data dimensionality.</li>"
                "<li>Hinton et al. 2012a. Deep neural networks for acoustic modeling.</li>"
                "<li>Graves et al. 2013. Speech recognition with deep recurrent neural networks.</li>"
                "<li>Tieleman and Hinton. 2012. Lecture 6.5 RMSProp.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual((references, citations), (6, 2))
        self.assertIn('href="#docling-reference-3">Hinton &amp; Salakhutdinov, 2006</a>', html_text)
        self.assertIn('href="#docling-reference-6">Tieleman &amp; Hinton, 2012</a>', html_text)
        self.assertIn('href="#docling-reference-1">Deng et al., 2013</a>', html_text)

    def test_bibliography_links_partial_author_year_group_with_unresolved_tail(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Other approaches modify training "
                        "(Wiesler et al., 2014; Raiko et al., 2012; "
                        "Povey et al., 2014; Desjardins & Kavukcuoglu)."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Wiesler, Simon and Ney, Hermann. 2014. Mean-normalized stochastic gradient.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Raiko, Tapani, Valpola, Harri, and LeCun, Yann. 2012. Deep learning made easier.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Povey, Daniel, Zhang, Xiaohui, and Khudanpur, Sanjeev. 2014. Parallel training.", "prov": [{"page_no": 2}]},
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Other approaches modify training "
                "(Wiesler et al., 2014; Raiko et al., 2012; "
                "Povey et al., 2014; Desjardins &amp; Kavukcuoglu).</p>"
                "<h2>References</h2><ol>"
                "<li>Wiesler, Simon and Ney, Hermann. 2014. Mean-normalized stochastic gradient.</li>"
                "<li>Raiko, Tapani, Valpola, Harri, and LeCun, Yann. 2012. Deep learning made easier.</li>"
                "<li>Povey, Daniel, Zhang, Xiaohui, and Khudanpur, Sanjeev. 2014. Parallel training.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 1)
        self.assertEqual((references, citations), (3, 1))
        self.assertIn('href="#docling-reference-1">Wiesler et al., 2014</a>', html_text)
        self.assertIn('href="#docling-reference-2">Raiko et al., 2012</a>', html_text)
        self.assertIn('href="#docling-reference-3">Povey et al., 2014</a>', html_text)
        self.assertIn("Desjardins &amp; Kavukcuoglu", html_text)

    def test_bibliography_disambiguates_et_al_from_same_author_year(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Speech models improved rapidly (Graves et al., 2013).",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "Graves, Alex. Generating sequences with recurrent neural networks. arXiv preprint arXiv:1308.0850, 2013.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Graves, Alex, Mohamed, Abdel-rahman, and Hinton, Geoffrey. Speech recognition with deep recurrent neural networks. ICASSP, 2013.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Speech models improved rapidly (Graves et al., 2013).</p>"
                "<h2>References</h2><ol>"
                "<li>Graves, Alex. Generating sequences with recurrent neural networks. arXiv preprint arXiv:1308.0850, 2013.</li>"
                "<li>Graves, Alex, Mohamed, Abdel-rahman, and Hinton, Geoffrey. Speech recognition with deep recurrent neural networks. ICASSP, 2013.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual((references, citations), (2, 1))
        self.assertIn('href="#docling-reference-2">Graves et al., 2013</a>', html_text)

    def test_bibliography_accepts_reference_entries_labeled_as_text(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Inception was used for the benchmark (Szegedy et al., 2014).",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "text",
                    "text": "Szegedy, Christian, Liu, Wei, Jia, Yangqing, et al. Going deeper with convolutions. CoRR, abs/1409.4842, 2014.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Inception was used for the benchmark (Szegedy et al., 2014).</p>"
                "<h2>References</h2>"
                "<p>Szegedy, Christian, Liu, Wei, Jia, Yangqing, et al. Going deeper with convolutions. CoRR, abs/1409.4842, 2014.</p>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["reference_count"], 1)
        self.assertEqual(diagnostics["citation_count"], 1)
        self.assertEqual((references, citations), (1, 1))
        self.assertIn('href="#docling-reference-1">Szegedy et al., 2014</a>', html_text)

    def test_bibliography_links_comma_separated_author_year_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "RNNs were widely used for NLP "
                        "[Hochreiter and Schmidhuber, 1997, Sutskever et al., 2014]."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "S. Hochreiter and J. Schmidhuber. Long short-term memory. Neural computation, 1997.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "I. Sutskever, O. Vinyals, and Q. V. Le. Sequence to sequence learning. 2014.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>RNNs were widely used for NLP "
                "[Hochreiter and Schmidhuber, 1997, Sutskever et al., 2014].</p>"
                "<h2>References</h2><ol>"
                "<li>S. Hochreiter and J. Schmidhuber. Long short-term memory. Neural computation, 1997.</li>"
                "<li>I. Sutskever, O. Vinyals, and Q. V. Le. Sequence to sequence learning. 2014.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 1)
        self.assertEqual(diagnostics["linked_number_count"], 2)
        self.assertEqual((references, citations), (2, 1))
        self.assertIn('href="#docling-reference-1">Hochreiter and Schmidhuber, 1997</a>', html_text)
        self.assertIn('href="#docling-reference-2">Sutskever et al., 2014</a>', html_text)

    def test_bibliography_allows_numeric_citation_after_year_with_space(self) -> None:
        document = {
            "texts": [
                {"label": "text", "text": "It started in 1952 [2], but O'=10[11] is an index.", "prov": [{"page_no": 1}]},
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "[2] The chemical basis of morphogenesis.", "orig": "[2] The chemical basis of morphogenesis.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "[11] A separate reference.", "orig": "[11] A separate reference.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>It started in 1952 [2], but O'=10[11] is an index.</p>"
                "<h2>References</h2><ul>"
                "<li>[2] The chemical basis of morphogenesis.</li>"
                "<li>[11] A separate reference.</li></ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 1)
        self.assertEqual((references, citations), (2, 1))
        self.assertIn('href="#docling-reference-2">2</a>', html_text)
        self.assertIn("O'=10[11]", html_text)

    def test_bibliography_keeps_numbered_section_headers_inside_references(self) -> None:
        document = {
            "texts": [
                {"label": "text", "text": "Later tools [24] and software [26] were shared [27,28].", "prov": [{"page_no": 1}]},
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "1. Early reference.", "orig": "1. Early reference.", "prov": [{"page_no": 2}]},
                {"label": "section_header", "text": "24. GCG software suite", "prov": [{"page_no": 3}]},
                {"label": "section_header", "text": "26. Sequence manipulation suites", "prov": [{"page_no": 3}]},
                {"label": "section_header", "text": "27. Software-sharing movement", "prov": [{"page_no": 3}]},
                {"label": "section_header", "text": "28. Open software culture", "prov": [{"page_no": 3}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Later tools [24] and software [26] were shared [27,28].</p>"
                "<h2>References</h2>"
                "<li>1. Early reference.</li>"
                "<h2>24. GCG software suite</h2>"
                "<h2>26. Sequence manipulation suites</h2>"
                "<h2>27. Software-sharing movement</h2>"
                "<h2>28. Open software culture</h2>"
            ),
            diagnostics,
        )

        self.assertEqual([item["number"] for item in diagnostics["references"]], [1, 24, 26, 27, 28])
        self.assertEqual(diagnostics["citation_count"], 3)
        self.assertEqual(diagnostics["linked_number_count"], 4)
        self.assertEqual((references, citations), (5, 3))
        self.assertIn('id="docling-reference-24"', html_text)
        self.assertIn('href="#docling-reference-26">26</a>', html_text)

    def test_appendix_mentions_link_to_existing_appendix_heading(self) -> None:
        html_text, count = adapter._link_appendix_references_in_html(
            (
                '<p>Prior work <a href="#docling-reference-1">Wang et al., 2018a</a>. '
                "Detailed descriptions are included in Appendix B.1.</p>"
                "<h2>B.1 Detailed Descriptions for the GLUE Benchmark Experiments.</h2>"
            )
        )

        self.assertEqual(count, 1)
        self.assertIn('id="docling-appendix-b-1"', html_text)
        self.assertIn('<a href="#docling-reference-1">Wang et al., 2018a</a>', html_text)
        self.assertIn('href="#docling-appendix-b-1">Appendix B.1</a>', html_text)

    def test_formula_second_pass_removes_adjacent_original_mathml_duplicate(self) -> None:
        html_text, count = adapter._remove_adjacent_original_formula_duplicates(
            (
                '<div class="docling-formula-second-pass" data-formula-index="2">'
                "<div>Formula 2</div></div>\n"
                '<div><math display="block"><mi>q</mi></math></div>'
                "<p>Body text</p>"
                '<div><math display="block"><mi>x</mi></math></div>'
            ),
            {2},
        )

        self.assertEqual(count, 1)
        self.assertNotIn("<mi>q</mi>", html_text)
        self.assertIn("<mi>x</mi>", html_text)

    def test_html_superscript_note_candidate_tolerates_marker_spacing(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Yelong Shen ∗ Shean Wang",
                    "prov": [{"page_no": 1}],
                }
            ]
        }
        notes = [{"page_no": 1, "marker": "*", "note_id": "docling-note-p1-star-1"}]
        candidates = adapter._html_inline_note_references(
            document,
            '<p>Yelong Shen<sup class="docling-footnote-ref">∗</sup>Shean Wang</p>',
            notes,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["marker"], "*")
        self.assertEqual(candidates[0]["source"], "final_html_sup_element_and_same_page_node")

    def test_empty_table_with_caption_uses_source_crop_as_visible_fallback(self) -> None:
        document = {
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "caption",
                    "text": "Figure 1. Presented table.",
                    "prov": [{"page_no": 1}],
                }
            ],
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "label": "table",
                    "captions": [{"$ref": "#/texts/0"}],
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 100, "b": 50}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "tables").mkdir()
            (output_dir / "tables" / "table_1.png").write_bytes(b"png")
            (output_dir / "document.html").write_text(
                "<table><caption><div class=\"caption\">"
                "Figure 1. Presented table.</div></caption></table>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "Figure 1. Presented table.\n",
                encoding="utf-8",
            )

            result = adapter.inject_empty_table_visual_fallbacks(
                output_dir,
                document,
                document["tables"],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_applied_count"], 1)
        self.assertEqual(result["markdown_applied_count"], 1)
        self.assertIn("docling-table-visual-fallback", html_text)
        self.assertIn("tables/table_1.png", html_text)
        self.assertIn("![Figure 1. Presented table.](tables/table_1.png)", markdown)

    def test_structured_table_keeps_grid_and_appends_exact_source_rendering(self) -> None:
        document = {
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "caption",
                    "text": "Table 1. Results.",
                    "prov": [{"page_no": 1}],
                }
            ],
            "tables": [
                {
                    "label": "table",
                    "captions": [{"$ref": "#/texts/0"}],
                    "data": {"table_cells": [{"text": "A"}], "num_rows": 1, "num_cols": 1},
                    "prov": [{"page_no": 1}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "tables").mkdir()
            (output_dir / "tables" / "table_1.png").write_bytes(b"png")
            (output_dir / "document.html").write_text(
                "<html><body><table><tr><td>A</td></tr></table></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "| A |\n|---|\n| 1 |\n",
                encoding="utf-8",
            )

            result = adapter.append_structured_table_source_renderings(
                output_dir,
                document,
                document["tables"],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_applied_count"], 1)
        self.assertIn("<table><tr><td>A</td></tr></table>", html_text)
        self.assertIn("docling-table-source-evidence", html_text)
        self.assertIn("tables/table_1.png", markdown)

    def test_chunk_merge_adds_no_visible_page_range_heading(self) -> None:
        merged = adapter.merge_chunk_responses(
            [
                {
                    "page_range": [1, 2],
                    "response": {
                        "status": "success",
                        "document": {
                            "md_content": "Body",
                            "html_content": "<p>Body</p>",
                            "text_content": "Body",
                            "json_content": {"texts": []},
                        },
                    },
                }
            ],
            source_label="transient-recovery",
        )

        html_text = merged["document"]["html_content"]
        self.assertNotIn("<h2>Pages", html_text)
        self.assertIn('data-page-range="[1, 2]"', html_text)

    def test_footnote_superscript_polish_does_not_split_adjacent_words(self) -> None:
        updated, count = adapter._polish_footnote_superscripts(
            "<p>Yelong Shen ∗ Shean Wang</p>"
        )

        self.assertEqual(count, 1)
        self.assertIn("Shen<sup", updated)
        self.assertIn("</sup>Shean", updated)
        self.assertNotIn("She n", updated)

    def test_author_region_reorders_misplaced_author_and_splits_body_tail(self) -> None:
        document = {
            "texts": [
                {"label": "section_header", "text": "TITLE", "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 300, "t": 700, "b": 680}}]},
                {"label": "section_header", "text": "Petar *", "prov": [{"page_no": 1, "bbox": {"l": 60, "r": 150, "t": 660, "b": 650}}]},
                {"label": "text", "text": "Department A", "prov": [{"page_no": 1, "bbox": {"l": 60, "r": 180, "t": 640, "b": 630}}]},
                {"label": "text", "text": "Guillem * Centre B", "prov": [{"page_no": 1, "bbox": {"l": 320, "r": 500, "t": 660, "b": 645}}]},
                {"label": "text", "text": "Department A", "prov": [{"page_no": 1, "bbox": {"l": 320, "r": 500, "t": 650, "b": 640}}]},
                {"label": "text", "text": "g@example.org based on its state in every layer.", "prov": [{"page_no": 1, "bbox": {"l": 320, "r": 500, "t": 640, "b": 630}}]},
                {"label": "section_header", "text": "ABSTRACT", "prov": [{"page_no": 1, "bbox": {"l": 250, "r": 350, "t": 500, "b": 490}}]},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                "<h1>TITLE</h1><h2>Petar *</h2><p>Department A</p>"
                "<h2>ABSTRACT</h2><p>Body.</p><p>Guillem * Centre B</p>"
                "<p>Department A</p>"
                "<p>g@example.org based on its state in every layer.</p>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "# TITLE\n\n## Petar <sup><a href=\"#note\">*</a></sup>\n\n"
                "Department A\n\n## ABSTRACT\n\nBody.\n\n"
                "**Guillem** <sup><a href=\"#note\">*</a></sup> Centre B\n\n"
                "Department A\n\n"
                "g@example.org based on its state in every layer.\n",
                encoding="utf-8",
            )

            result = adapter.recover_first_page_author_reading_order(
                output_dir,
                document,
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertEqual(result["author_record_count"], 5)
        self.assertEqual(result["markdown_record_replacement_count"], 5)
        self.assertLess(html_text.index("Guillem"), html_text.index("ABSTRACT"))
        self.assertLess(html_text.index("g@example.org"), html_text.index("ABSTRACT"))
        self.assertEqual(html_text.count("Department A"), 2)
        self.assertLess(markdown.index("**Guillem**"), markdown.index("## ABSTRACT"))
        self.assertIn('<a href="#note">*</a>', markdown)
        self.assertGreater(
            html_text.index("based on its state in every layer."),
            html_text.index("ABSTRACT"),
        )

    def test_first_page_abstract_reorders_before_two_column_frontmatter(self) -> None:
        document = {
            "texts": [
                {"label": "title", "text": "Title", "prov": [{"page_no": 1, "bbox": {"l": 75, "r": 530, "t": 700, "b": 670}}]},
                {"label": "section_header", "text": "CCS CONCEPTS", "prov": [{"page_no": 1, "bbox": {"l": 318, "r": 400, "t": 543, "b": 534}}]},
                {"label": "section_header", "text": "KEYWORDS", "prov": [{"page_no": 1, "bbox": {"l": 318, "r": 380, "t": 483, "b": 474}}]},
                {"label": "section_header", "text": "1 INTRODUCTION", "prov": [{"page_no": 1, "bbox": {"l": 318, "r": 420, "t": 346, "b": 337}}]},
                {"label": "section_header", "text": "ABSTRACT", "prov": [{"page_no": 1, "bbox": {"l": 54, "r": 112, "t": 543, "b": 534}}]},
                {
                    "label": "text",
                    "text": "Large language models are evaluated on structured table data.",
                    "prov": [{"page_no": 1, "bbox": {"l": 54, "r": 296, "t": 528, "b": 280}}],
                },
                {
                    "label": "text",
                    "text": "∗ Contribution note.",
                    "prov": [{"page_no": 1, "bbox": {"l": 54, "r": 296, "t": 255, "b": 239}}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                "<h1>Title</h1><h2>CCS CONCEPTS</h2><p>Concepts</p>"
                "<h2>KEYWORDS</h2><p>tables</p><h2>1 INTRODUCTION</h2><p>Intro.</p>"
                "<h2>ABSTRACT</h2><p>Large language models are evaluated on structured table data.</p>"
                "<p>∗ Contribution note.</p>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "# Title\n\n## CCS CONCEPTS\n\nConcepts\n\n## KEYWORDS\n\ntables\n\n"
                "## 1 INTRODUCTION\n\nIntro.\n\n## ABSTRACT\n\n"
                "Large language models are evaluated on structured table data.\n\n"
                "∗ Contribution note.\n",
                encoding="utf-8",
            )

            result = adapter.recover_first_page_abstract_reading_order(output_dir, document)
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertLess(html_text.index("ABSTRACT"), html_text.index("CCS CONCEPTS"))
        self.assertLess(markdown.index("## ABSTRACT"), markdown.index("## CCS CONCEPTS"))
        self.assertGreater(html_text.index("∗ Contribution note."), html_text.index("1 INTRODUCTION"))

    def test_semantic_emphasis_uses_pdf_font_evidence(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Normal Important result.",
                    "formatting": None,
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 10, "r": 200, "t": 100, "b": 80},
                        }
                    ],
                }
            ]
        }
        source = {
            "available": True,
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": [
                        {
                            "index": index,
                            "text": char,
                            "bbox": {"l": 70 + index, "r": 71 + index, "t": 95, "b": 85},
                            "font_name": "Example-Bold",
                            "font_weight": 700,
                            "font_size": 10,
                        }
                        for index, char in enumerate("Important")
                    ],
                }
            },
        }

        diagnostics = adapter.semantic_emphasis_diagnostics(document, source)
        html_text, html_count = adapter._apply_semantic_spans_to_html(
            "<html><body><p>Normal Important result.</p></body></html>",
            diagnostics,
        )
        markdown, markdown_count = adapter._apply_semantic_spans_to_markdown(
            "Normal Important result.\n",
            diagnostics,
        )

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(document["texts"][0]["formatting"]["semantic_spans"][0]["styles"], ["bold"])
        self.assertEqual(html_count, 1)
        self.assertIn("<strong>Important</strong>", html_text)
        self.assertEqual(markdown_count, 1)
        self.assertIn("**Important**", markdown)

    def test_semantic_emphasis_treats_medium_font_as_bold(self) -> None:
        self.assertIn("bold", adapter._font_semantic_styles("NimbusRomNo9L-Medi", None))
        self.assertIn("bold", adapter._font_semantic_styles("Example-Medium", None))

    def test_semantic_emphasis_avoids_nested_markdown_spans(self) -> None:
        diagnostics = [
            {
                "page_no": 1,
                "node_text": "From RNNs to Transformers body.",
                "text": "From RNNs to Transformers",
                "start": 0,
                "end": 25,
                "styles": ["bold"],
            },
            {
                "page_no": 1,
                "node_text": "From RNNs to Transformers body.",
                "text": "RNN",
                "start": 5,
                "end": 8,
                "styles": ["bold"],
            },
        ]

        markdown, count = adapter._apply_semantic_spans_to_markdown(
            "From RNNs to Transformers body.\n",
            diagnostics,
        )

        self.assertEqual(count, 1)
        self.assertIn("**From RNNs to Transformers** body.", markdown)
        self.assertNotIn("**From **RNN**s", markdown)

    def test_structural_quarantine_matches_markdown_escaped_punctuation(self) -> None:
        text = "AUTHORIZATION MD.! _ MP-75"
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 700, "b": 520},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": text,
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 208, "r": 251, "t": 767, "b": 762},
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "AUTHORIZATION MD.! \\_ MP-75\n",
                encoding="utf-8",
            )
            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["markdown_quarantine_replacement_count"], 1)
        markdown_body = adapter._markdown_without_structural_content(markdown)
        self.assertNotIn(
            "AUTHORIZATION MD",
            adapter.re.sub(r"<!--.*?-->", "", markdown_body),
        )
        self.assertEqual(result["final_output_residual_count"], 0)

    def test_structural_quarantine_removes_figure_diagram_label_clusters(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Nx",
                    "prov": [{"page_no": 1, "bbox": {"l": 15, "r": 28, "t": 605, "b": 597}}],
                },
                {
                    "label": "text",
                    "text": "Encoding Add & Norm Feed Forward Add & Norm Multi-Head Attention Input Embedding",
                    "prov": [{"page_no": 1, "bbox": {"l": 9, "r": 47, "t": 533, "b": 523}}],
                },
                {
                    "label": "text",
                    "text": "Inputs Output Probabilities Softmax",
                    "prov": [{"page_no": 1, "bbox": {"l": 67, "r": 92, "t": 479, "b": 471}}],
                },
                {
                    "label": "caption",
                    "text": "Figure 1: The Transformer - model architecture.",
                    "prov": [{"page_no": 1, "bbox": {"l": 108, "r": 504, "t": 390, "b": 370}}],
                },
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [{"page_no": 1, "bbox": {"l": 195, "r": 417, "t": 719, "b": 398}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                "<html><body><p>Nx</p>"
                "<p>Encoding Add &amp; Norm Feed Forward Add &amp; Norm Multi-Head Attention Input Embedding</p>"
                "<p>Inputs Output Probabilities Softmax</p>"
                "<figure><figcaption>Figure 1: The Transformer - model architecture.</figcaption></figure>"
                "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "Nx\n\nEncoding Add &amp; Norm Feed Forward Add &amp; Norm Multi-Head Attention Input Embedding\n\n"
                "Inputs Output Probabilities Softmax\n\nFigure 1: The Transformer - model architecture.\n",
                encoding="utf-8",
            )

            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            body_html = adapter._html_without_structural_content(
                (output_dir / "document.html").read_text(encoding="utf-8")
            )
            markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )
            content = json.loads((output_dir / "structural_content.json").read_text(encoding="utf-8"))

        self.assertGreaterEqual(result["html_quarantine_replacement_count"], 3)
        self.assertGreaterEqual(result["markdown_quarantine_replacement_count"], 3)
        self.assertNotIn("<p>Nx</p>", body_html)
        self.assertNotIn("Encoding Add", body_html)
        self.assertNotIn("Encoding Add", markdown)
        self.assertIn("Figure 1: The Transformer", body_html)
        self.assertTrue(any(item["kind"] == "visual_annotation" for item in content["records"]))

    def test_structural_quarantine_removes_private_use_math_caption_prefix(self) -> None:
        text = "   Figure 1: Left: Schematic depiction of a model."
        document = {
            "texts": [
                {
                    "label": "caption",
                    "text": text,
                    "prov": [{"page_no": 1, "bbox": {"l": 108, "r": 506, "t": 553, "b": 500}}],
                }
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [{"page_no": 1, "bbox": {"l": 113, "r": 501, "t": 704, "b": 565}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(text + "\n", encoding="utf-8")

            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            body_html = adapter._html_without_structural_content(
                (output_dir / "document.html").read_text(encoding="utf-8")
            )
            markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )
            content = json.loads((output_dir / "structural_content.json").read_text(encoding="utf-8"))

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertNotIn("", body_html)
        self.assertNotIn("", markdown)
        self.assertIn("Figure 1: Left", body_html)
        self.assertTrue(any(item["kind"] == "math_font_noise" for item in content["records"]))

    def test_structural_quarantine_removes_standalone_private_use_math_noise(self) -> None:
        text = ""
        document = {
            "texts": [
                {
                    "label": "quarantined_visual_annotation",
                    "text": text,
                    "prov": [{"page_no": 1, "bbox": {"l": 132, "r": 153, "t": 558, "b": 548}}],
                }
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [{"page_no": 1, "bbox": {"l": 113, "r": 501, "t": 704, "b": 565}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p><p>Figure 1: Model.</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(text + "\n\nFigure 1: Model.\n", encoding="utf-8")

            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            body_html = adapter._html_without_structural_content(
                (output_dir / "document.html").read_text(encoding="utf-8")
            )
            markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )
            content = json.loads((output_dir / "structural_content.json").read_text(encoding="utf-8"))

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertNotIn(text, body_html)
        self.assertNotIn(text, markdown)
        self.assertTrue(any(item["kind"] == "math_font_noise" for item in content["records"]))

    def test_structural_quarantine_does_not_relabel_formula_nodes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (24)",
                    "prov": [{"page_no": 2, "bbox": {"l": 80, "r": 360, "t": 92, "b": 82}}],
                },
                {
                    "label": "text",
                    "text": "1 Correspondence to: author@example.org",
                    "prov": [{"page_no": 2, "bbox": {"l": 80, "r": 360, "t": 72, "b": 62}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "formula")
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote_candidate")

    def test_structural_quarantine_preserves_full_long_footnote_text(self) -> None:
        long_text = (
            "Permission to make digital or hard copies of all or part of this work "
            "for personal or classroom use is granted without fee provided that "
            "copies are not made or distributed for profit or commercial advantage "
            "and that copies bear this notice and the full citation on the first page. "
            "Copyrights for components of this work owned by others than the author "
            "must be honored, and abstracting with credit is permitted."
        )
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": long_text,
                    "prov": [{"page_no": 1, "bbox": {"l": 40, "r": 540, "t": 190, "b": 170}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(qc["candidates"][0]["text"], long_text)
        self.assertLess(len(qc["candidates"][0]["text_preview"]), len(long_text))

    def test_structural_quarantine_marks_marker_led_contribution_footnote(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "∗ Equal contributions during internship at Microsoft Research Asia.",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 243, "t": 188, "b": 180}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_footnote_candidate")
        self.assertIn("marker_led_footnote_content_candidate", qc["candidates"][0]["reasons"])

    def test_structural_quarantine_extends_labeled_footnote_cluster_to_marker_line(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "1 Code and data are available at https://example.org/project",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 250, "t": 175, "b": 164}}],
                },
                {
                    "label": "footnote",
                    "text": "Permission to make copies is granted.",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 300, "t": 158, "b": 116}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        marker_candidate = next(
            item for item in qc["candidates"] if item["text"].startswith("1 Code")
        )
        self.assertEqual(marker_candidate["action"], "quarantine_from_main_text_flow")
        self.assertIn("same_column_footnote_cluster", marker_candidate["reasons"])
        self.assertEqual(document["texts"][0]["label"], "quarantined_footnote_candidate")

    def test_structural_quarantine_extends_labeled_footnote_cluster_to_unmarked_continuation(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "1 Please find code at https://example.com/repo.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 290, "t": 230, "b": 214}}],
                },
                {
                    "label": "text",
                    "text": "Please note that the private preview may be replaced by an official one at https://github.com/example/project.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 290, "t": 211, "b": 198}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)
        continuation = next(item for item in qc["candidates"] if item["text"].startswith("Please note"))

        self.assertEqual(continuation["kind"], "footnote_candidate")
        self.assertEqual(continuation["confidence"], "high")
        self.assertIn("same_column_footnote_continuation", continuation["reasons"])
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote_candidate")

    def test_structural_quarantine_removes_top_edge_ocr_adjacent_to_empty_tables(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Windermere Area",
                    "prov": [{"page_no": 2, "bbox": {"l": 10, "r": 85, "t": 770, "b": 760}}],
                },
                {
                    "label": "text",
                    "text": "Normal body paragraph.",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 540, "t": 500, "b": 470}}],
                },
            ],
            "tables": [
                {
                    "label": "table",
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                    "prov": [{"page_no": 2, "bbox": {"l": 60, "r": 150, "t": 706, "b": 668}}],
                },
                {
                    "label": "table",
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                    "prov": [{"page_no": 2, "bbox": {"l": 170, "r": 270, "t": 706, "b": 668}}],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_table_visual_annotation")
        self.assertEqual(document["texts"][1]["label"], "text")
        self.assertIn(
            "text_bbox_inside_or_adjacent_to_table",
            qc["candidates"][0]["reasons"],
        )

    def test_structural_quarantine_preserves_first_page_affiliation_mislabels(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "2 University of Example, Department of AI",
                    "prov": [{"page_no": 1, "bbox": {"l": 90, "r": 410, "t": 650, "b": 630}}],
                },
                {
                    "label": "footnote",
                    "text": "5 机构智能实验室",
                    "prov": [{"page_no": 1, "bbox": {"l": 90, "r": 320, "t": 625, "b": 605}}],
                },
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [{"page_no": 1, "bbox": {"l": 120, "r": 124, "t": 90, "b": 85}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "text")
        self.assertEqual(document["texts"][1]["label"], "text")
        self.assertEqual(document["texts"][2]["label"], "quarantined_footnote")
        self.assertIn("author_affiliation_recovery", document["texts"][0]["local_ai_lab_qc"])

    def test_affiliation_recovery_does_not_preserve_contribution_footnotes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "∗ Equal contributions during internship at Microsoft Research Asia.",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 243, "t": 188, "b": 180}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_footnote")
        self.assertNotIn("author_affiliation_recovery", document["texts"][0].get("local_ai_lab_qc", {}))

    def test_recovers_fragmented_first_page_affiliations_from_pdf_text_layer(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "OCR-free Document Understanding Transformer",
                    "prov": [{"page_no": 1, "bbox": {"t": 580, "b": 568}}],
                },
                {
                    "label": "text",
                    "text": "Geewook Kim 1 ∗ , Teakgyu Hong 4 †",
                    "prov": [{"page_no": 1, "bbox": {"t": 545, "b": 509}}],
                },
                {
                    "label": "text",
                    "text": "2",
                    "prov": [{"page_no": 1, "bbox": {"t": 498, "b": 493}}],
                },
                {
                    "label": "text",
                    "text": "3 NAVER AI Lab ut ut ut",
                    "prov": [{"page_no": 1, "bbox": {"t": 498, "b": 489}}],
                },
                {
                    "label": "text",
                    "text": "1 NAVER CLOVA",
                    "prov": [{"page_no": 1, "bbox": {"t": 498, "b": 489}}],
                },
                {
                    "label": "text",
                    "text": "5",
                    "prov": [{"page_no": 1, "bbox": {"t": 487, "b": 482}}],
                },
                {
                    "label": "text",
                    "text": "4 Upstage NAVER Search Tmax 6 Google 7 LBox",
                    "prov": [{"page_no": 1, "bbox": {"t": 487, "b": 478}}],
                },
                {
                    "label": "text",
                    "text": "Abstract. Body",
                    "prov": [{"page_no": 1, "bbox": {"t": 439, "b": 223}}],
                },
            ]
        }
        original_pdf_text = adapter._first_page_pdf_text
        adapter._first_page_pdf_text = lambda _path: (
            "OCR-free Document Understanding Transformer\n"
            "Geewook Kim1∗\n"
            "1NAVER CLOVA 2NAVER Search 3NAVER AI Lab\n"
            "4Upstage 5Tmax 6Google 7LBox\n"
            "Abstract. Body\n"
        )
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir)
                (out / "document.md").write_text(
                    "## OCR-free Document Understanding Transformer\n\n"
                    "Geewook Kim 1 ∗ , Teakgyu Hong 4 †\n\n"
                    "2\n\n3 NAVER AI Lab ut ut ut\n\n1 NAVER CLOVA\n\n5\n\n"
                    "4 Upstage NAVER Search Tmax 6 Google 7 LBox\n\nAbstract. Body\n",
                    encoding="utf-8",
                )
                (out / "document.html").write_text(
                    "<html><body><p>Geewook Kim 1 ∗ , Teakgyu Hong 4 †</p>"
                    "<p>2</p><p>3 NAVER AI Lab ut ut ut</p><p>1 NAVER CLOVA</p>"
                    "<p>5</p><p>4 Upstage NAVER Search Tmax 6 Google 7 LBox</p>"
                    "<p>Abstract. Body</p></body></html>",
                    encoding="utf-8",
                )
                result = adapter.recover_first_page_author_affiliations(
                    out,
                    document,
                    Path("dummy.pdf"),
                )
                md_text = (out / "document.md").read_text(encoding="utf-8")
                html_text = (out / "document.html").read_text(encoding="utf-8")
        finally:
            adapter._first_page_pdf_text = original_pdf_text

        self.assertTrue(result["applied"])
        self.assertIn("1 NAVER CLOVA 2 NAVER Search 3 NAVER AI Lab", md_text)
        self.assertIn("4 Upstage 5 Tmax 6 Google 7 LBox", md_text)
        self.assertIn("docling-author-affiliation-recovery", html_text)
        self.assertEqual(document["texts"][2]["text"].splitlines()[0], "1 NAVER CLOVA 2 NAVER Search 3 NAVER AI Lab")
        self.assertEqual(document["texts"][3]["label"], "quarantined_author_affiliation_fragment")

    def test_replace_exact_paragraph_with_quarantine_hides_text_from_render_flow(self) -> None:
        item = {
            "kind": "page_header",
            "text": "arXiv:2506.22084v1 [cs.LG]",
            "page_no": 1,
            "reasons": ["publication_template_noise"],
        }
        html, changed = adapter._replace_exact_paragraph_with_quarantine(
            "<html><body><p>Body text</p><p><span>arXiv:2506.22084v1 [cs.LG]</span></p></body></html>",
            item,
        )

        self.assertTrue(changed)
        self.assertIn("<!-- local-ai-lab structural quarantine", html)
        self.assertNotIn("<span>arXiv:2506.22084v1 [cs.LG]</span>", html)
        self.assertIn("evidence=structural_regions.json", html)

    def test_embedded_visual_ocr_noise_is_hidden_from_main_flow(self) -> None:
        long_noise = "A" * 520 + "0" * 80

        html_text, html_count = adapter._replace_embedded_visual_ocr_noise_blocks_html(
            f"<html><body><p>Body remains readable.</p><p>axis {long_noise}</p></body></html>"
        )
        markdown, md_count = adapter._replace_embedded_visual_ocr_noise_blocks_markdown(
            f"Body remains readable.\n\naxis {long_noise}\n"
        )

        self.assertEqual(html_count, 1)
        self.assertEqual(md_count, 1)
        self.assertIn("Body remains readable", html_text)
        self.assertIn("embedded_visual_ocr_noise", html_text)
        self.assertIn("embedded_visual_ocr_noise", markdown)

    def test_author_email_prefix_is_split_from_algorithm_caption(self) -> None:
        html_text, html_count = adapter._split_author_affiliation_from_body_html(
            "<p><strong>Jimmy Lei Ba</strong> University jimmy@example.edu "
            "Algorithm 1: Adam, our proposed algorithm.</p>"
        )
        markdown, md_count = adapter._split_author_affiliation_from_body_markdown(
            "**Jimmy Lei Ba** University jimmy@example.edu Algorithm 1: Adam, our proposed algorithm."
        )

        self.assertEqual(html_count, 1)
        self.assertEqual(md_count, 1)
        self.assertIn("author_affiliation_fragment", html_text)
        self.assertIn("<p>Algorithm 1: Adam", html_text)
        self.assertNotIn("Jimmy Lei Ba</strong> University", html_text)
        self.assertIn("Algorithm 1: Adam", markdown)

    def test_algorithm_code_blocks_gain_readable_line_breaks(self) -> None:
        html_text, count = adapter._normalize_algorithm_code_blocks_html(
            "<pre><code>Require: α : Stepsize Require: β 1 : Rate "
            "m 0 ← 0 while θ t not converged do t ← t + 1 return θ t</code></pre>"
        )

        self.assertEqual(count, 1)
        self.assertIn("\nRequire: β", html_text)
        self.assertIn("\nwhile θ", html_text)

    def test_algorithm_recovery_uses_pdf_bbox_text_layer(self) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "algorithm.pdf"
            doc = fitz.open()
            page = doc.new_page(width=300, height=400)
            page.insert_text((50, 80), "Require: alpha: Stepsize", fontsize=10)
            page.insert_text((50, 96), "while theta not converged do", fontsize=10)
            page.insert_text((60, 112), "theta <- theta + 1", fontsize=10)
            page.insert_text((50, 128), "return theta", fontsize=10)
            doc.save(pdf_path)
            doc.close()
            document = {
                "texts": [
                    {
                        "label": "code",
                        "text": "Require: alpha: Stepsize while theta not converged do theta <- theta + 1 return theta",
                        "prov": [
                            {
                                "page_no": 1,
                                "bbox": {
                                    "l": 45,
                                    "t": 325,
                                    "r": 250,
                                    "b": 260,
                                    "coord_origin": "BOTTOMLEFT",
                                },
                            }
                        ],
                    }
                ]
            }

            records = adapter._algorithm_candidate_records(document, pdf_path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "pdf_text_bbox")
        self.assertIn("Require: alpha", records[0]["text"])
        self.assertIn("\nwhile theta", records[0]["text"])
        self.assertIsInstance(records[0].get("layout"), dict)
        self.assertGreaterEqual(records[0]["layout"]["line_count"], 4)
        self.assertGreaterEqual(records[0]["layout"]["distinct_indent_count"], 2)

    def test_algorithm_candidate_prefers_shared_source_geometry_reflow(self) -> None:
        document = {
            "texts": [
                {
                    "label": "code",
                    "text": (
                        "Require: Input x; Ensure: x_out in R "
                        "1: initialize stream 2: return x_out"
                    ),
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 250,
                                "t": 300,
                                "b": 200,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "source.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            with (
                patch.object(
                    adapter,
                    "source_algorithm_block",
                    return_value=(
                        "Algorithm 1 Shared source geometry",
                        "Require: Input x\nEnsure: x_{out} in R^{B}\n"
                        "1   initialize stream\n2   return x_{out}",
                    ),
                ),
                patch.object(
                    adapter,
                    "_pdf_text_for_bbox",
                    return_value=(
                        "Require: Input x Ensure: x out in R "
                        "1 initialize stream 2 return x out"
                    ),
                ),
                patch.object(
                    adapter,
                    "_pdf_algorithm_layout_for_bbox",
                    return_value={
                        "line_count": 4,
                        "lines": [
                            {"text": "x"},
                            {"text": "out"},
                            {"text": "R"},
                            {"text": "B"},
                        ],
                    },
                ),
            ):
                records = adapter._algorithm_candidate_records(document, pdf_path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "source_geometry_reflow")
        self.assertIsNone(records[0]["layout"])
        self.assertEqual(
            records[0]["text"],
            "Require: Input x\nEnsure: x_{out} in R^{B}\n"
            "1   initialize stream\n2   return x_{out}",
        )

    def test_algorithm_layout_html_preserves_span_styles_and_offsets(self) -> None:
        html_text = adapter._algorithm_layout_html(
            {
                "layout": {
                    "source": "pdf_text_span_layout",
                    "line_count": 4,
                    "span_count": 5,
                    "lines": [
                        {
                            "text": "Algorithm 1: Adam, our proposed algorithm.",
                            "indent_px": 0,
                            "spans": [{"text": "Algorithm 1: Adam, our proposed algorithm.", "styles": []}],
                        },
                        {
                            "text": "Require: alpha",
                            "indent_px": 0,
                            "spans": [
                                {"text": "Require:", "styles": ["bold"]},
                                {"text": " alpha", "styles": []},
                            ],
                        },
                        {
                            "text": "return theta",
                            "indent_px": 14,
                            "spans": [
                                {"text": "return", "styles": ["bold", "italic"]},
                                {"text": " theta", "styles": []},
                            ],
                        },
                        {
                            "text": "end",
                            "indent_px": 0,
                            "spans": [
                                {"text": "end", "styles": ["bold"]},
                            ],
                        },
                    ],
                }
            }
        )

        self.assertIn("docling-algorithm-layout-block", html_text)
        self.assertIn("padding-left:2.00ch", html_text)
        self.assertIn("docling-algorithm-span-bold", html_text)
        self.assertIn("docling-algorithm-span-italic", html_text)
        self.assertNotIn("Algorithm 1: Adam", html_text)

    def test_algorithm_fragmented_layout_falls_back_to_semantic_text(self) -> None:
        record = {
            "layout": {
                "source": "pdf_text_span_layout",
                "line_count": 12,
                "span_count": 12,
                "lines": [
                    {"text": "Input: values", "indent_px": 0, "spans": [{"text": "Input: values", "styles": ["bold"]}]},
                    {"text": "m", "indent_px": 52, "spans": [{"text": "m", "styles": []}]},
                    {"text": "X", "indent_px": 50, "spans": [{"text": "X", "styles": []}]},
                    {"text": "m", "indent_px": 38, "spans": [{"text": "m", "styles": []}]},
                    {"text": "i=1", "indent_px": 51, "spans": [{"text": "i=1", "styles": []}]},
                    {"text": "xi", "indent_px": 65, "spans": [{"text": "xi", "styles": []}]},
                    {"text": "// mini-batch mean", "indent_px": 144, "spans": [{"text": "// mini-batch mean", "styles": []}]},
                    {"text": "p", "indent_px": 38, "spans": [{"text": "p", "styles": []}]},
                    {"text": "B + eps", "indent_px": 54, "spans": [{"text": "B + eps", "styles": []}]},
                    {"text": "// normalize", "indent_px": 181, "spans": [{"text": "// normalize", "styles": []}]},
                ],
            }
        }

        html_text = adapter._algorithm_layout_html(record)

        self.assertEqual(html_text, "")
        self.assertIn("layout_fragmented_short_line_ratio", record["layout_visible_fallback_reasons"])

    def test_algorithm_semantic_layout_renders_indented_lines(self) -> None:
        html_text = adapter._algorithm_semantic_layout_html(
            "1: initialize\n"
            "2: for k = 1 .. K do\n"
            "  3: update state\n"
            "  4: end for"
        )

        self.assertIn("docling-algorithm-semantic-layout", html_text)
        self.assertIn("padding-left:2.00ch", html_text)
        self.assertIn("docling-algorithm-keyword", html_text)

    def test_algorithm_semantic_layout_renders_formula_lines_as_math(self) -> None:
        html_text = adapter._algorithm_semantic_layout_html(
            r"Update the discriminator:" "\n"
            r"  \nabla _ { \theta _ { d } } \frac { 1 } { m } \sum _ { i = 1 } ^ { m } \log D ( x_i )"
        )

        self.assertIn("docling-algorithm-formula-line", html_text)
        self.assertIn(r"\(", html_text)
        self.assertIn(r"\nabla", html_text)

    def test_algorithm_semantic_layout_repairs_formula_ocr_artifacts(self) -> None:
        html_text = adapter._algorithm_semantic_layout_html(
            r"  \nabla _ { \theta _ { d } } \log D \left ( \pm b { x } ^ { ( i ) } \right )"
        )

        self.assertIn("docling-algorithm-formula-line", html_text)
        self.assertIn(r"\pm b", html_text)
        self.assertNotIn(r"\mathbf { x }", html_text)

    def test_algorithm_html_replacement_does_not_swallow_following_sections(self) -> None:
        html_text = (
            "<p>Algorithm 1 Training loop.</p>"
            '<div class="docling-formula-second-pass" data-formula-index="2">'
            r"<div>\[\nabla D(x)\]</div></div>"
            "<h2>4.1 Global Optimality of p g = p data</h2>"
            "<p>Proposition 1. For fixed G, the optimal discriminator is important.</p>"
            '<div class="docling-formula-second-pass" data-formula-index="3">'
            r"<div>\[\nabla G(z)\]</div></div>"
        )
        records = [
            {
                "label": "Algorithm 1 Training loop.",
                "text": "for number of training iterations do\n  update discriminator\n  update generator",
                "original_text": r"Algorithm 1 Training loop. \nabla D(x) \nabla G(z)",
                "source": "docling_algorithm_cluster",
                "formula_nos": [2, 3],
                "html_targets": ["Algorithm 1 Training loop."],
                "original_label": "algorithm_cluster",
            }
        ]

        updated, changed = adapter._replace_algorithm_records_in_html(html_text, records)

        self.assertEqual(changed, 1)
        self.assertIn("docling-algorithm-recovered", updated)
        self.assertIn("4.1 Global Optimality", updated)
        self.assertIn("Proposition 1", updated)

    def test_algorithm_markdown_replacement_does_not_swallow_following_sections(self) -> None:
        md_text = (
            "Algorithm 1 Training loop.\n\n"
            "$$\\nabla D(x)$$\n\n"
            "## 4.1 Global Optimality\n\n"
            "Proposition 1. For fixed G, the optimal discriminator is important.\n\n"
            "$$\\nabla G(z)$$\n"
        )
        records = [
            {
                "label": "Algorithm 1 Training loop.",
                "text": "for number of training iterations do\n  update discriminator\n  update generator",
                "original_text": r"Algorithm 1 Training loop. \nabla D(x) \nabla G(z)",
                "source": "docling_algorithm_cluster",
                "formula_nos": [1, 2],
                "html_targets": ["Algorithm 1 Training loop."],
                "original_label": "algorithm_cluster",
            }
        ]

        updated, changed = adapter._replace_algorithm_records_in_markdown(md_text, records)

        self.assertEqual(changed, 1)
        self.assertIn("Algorithm 1 Training loop", updated)
        self.assertIn("4.1 Global Optimality", updated)
        self.assertIn("Proposition 1", updated)

    def test_algorithm_formula_plain_text_preserves_fraction_structure(self) -> None:
        formula = (
            r"\begin{array} { l l } "
            r"\text {Input:} & \text {Values of $x$ over a mini-batch;} \\ "
            r"\text {Output:} & \{y_i = \text {BN}_{\gamma,\beta}(x_i)\} \\ "
            r"& \text {Input: $m$ over a mini-batch;} \\ "
            r"& \text {Output: $\mu_B$} \leftarrow \frac { 1 } { m } \sum_i x_i \\ "
            r"& \sigma_B^2 \leftarrow \frac { 1 } { m } \sum_i (x_i-\mu_B)^2 \quad "
            r"\text {// mini-batch variance} "
            r"\end{array}"
        )

        formatted = adapter._format_algorithm_text(adapter._algorithm_formula_plain_text(formula))

        self.assertIn(r"\frac { 1 } { m }", formatted)
        self.assertIn("// mini-batch variance", formatted)
        self.assertIn("Input: $m$ over", formatted)
        self.assertIn(r"\mu_B", formatted)

    def test_algorithm_lines_drop_duplicate_numbered_steps(self) -> None:
        formatted = adapter._format_algorithm_text(
            "8: for k = 1 \\dots K do\n"
            "10:\n"
            "10: Process multiple training mini-batches B, each of size m\n"
            "identity.\n"
            "11: replace the transform\n"
            "11: replace the transform duplicate\n"
            "12: end for"
        )

        self.assertIn("10: Process multiple", formatted)
        self.assertNotIn("10:\n", formatted)
        self.assertIn("identity.", formatted)
        self.assertIn("11: replace the transform", formatted)
        self.assertIn("11: replace the transform duplicate", formatted)
        self.assertEqual(
            [line.strip() for line in formatted.splitlines() if line.strip().startswith("11:")],
            ["11: replace the transform", "11: replace the transform duplicate"],
        )

    def test_algorithm_lines_keep_duplicate_assignments(self) -> None:
        formatted = adapter._format_algorithm_text(
            "1: x_1 ← 1\n"
            "2: x_1 ← 2\n"
        )

        self.assertEqual(formatted.count("x_1 ←"), 2)
        self.assertIn("1: x_1 ← 1", formatted)
        self.assertIn("2: x_1 ← 2", formatted)

    def test_algorithm_format_keeps_short_lines_and_io_sections(self) -> None:
        formatted = adapter._format_algorithm_text(
            "Algorithm 2: Demo example\n"
            "Input: training data D\n"
            "1: initialize model\n"
            "identity.\n"
            "Output: final weights\n"
            "Input: warm-start parameters"
        )

        self.assertIn("Input: training data D", formatted)
        self.assertIn("identity.", formatted)
        self.assertIn("Output: final weights", formatted)
        self.assertEqual(formatted.count("Input:"), 2)
        self.assertIn("Input: warm-start;", formatted)

    def test_algorithm_cluster_accepts_caption_without_colon_and_formula_steps(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Algorithm 1 Minibatch stochastic gradient descent training.",
                    "prov": [{"page_no": 4}],
                },
                {"label": "text", "text": "for number of training iterations do", "prov": [{"page_no": 4}]},
                {"label": "text", "text": "for k steps do", "prov": [{"page_no": 4}]},
                {"label": "list_item", "text": "Sample minibatch of m noise samples.", "prov": [{"page_no": 4}]},
                {"label": "list_item", "text": "Update the discriminator by ascending its stochastic gradient:", "prov": [{"page_no": 4}]},
                {
                    "label": "formula",
                    "text": r"\nabla_{\theta_d} \frac{1}{m}\sum_i \log D(x_i)",
                    "prov": [{"page_no": 4}],
                },
                {"label": "section_header", "text": "end for", "prov": [{"page_no": 4}]},
                {"label": "text", "text": "The gradient-based updates can use any standard rule.", "prov": [{"page_no": 4}]},
            ]
        }

        records = adapter._algorithm_candidate_records(document, Path("/nonexistent.pdf"))

        self.assertEqual(len(records), 1)
        self.assertIn("Algorithm 1 Minibatch", records[0]["label"])
        self.assertIn("for number of training iterations do", records[0]["text"])
        self.assertIn("    Update the discriminator", records[0]["text"])
        self.assertIn("  end for", records[0]["text"])
        self.assertEqual(records[0]["formula_nos"], [1])

    def test_algorithm_cluster_accepts_numbered_parameter_and_assignment_steps(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "Algorithm 2 Stochastic Proximal Point Method (SPPM)",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 200, "t": 300, "b": 285}}],
                },
                {
                    "label": "list_item",
                    "text": "1: Parameters: learning rate γ > 0",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 200, "t": 280, "b": 265}}],
                },
                {
                    "label": "list_item",
                    "text": "2: for k = 0, 1, 2, ... do",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 200, "t": 260, "b": 245}}],
                },
                {
                    "label": "list_item",
                    "text": "3: Sample ξ_k from D",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 200, "t": 240, "b": 225}}],
                },
                {
                    "label": "list_item",
                    "text": "4: x_{k+1} = prox(x_k)",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 200, "t": 220, "b": 205}}],
                },
                {
                    "label": "list_item",
                    "text": "5: end for",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 200, "t": 200, "b": 185}}],
                },
            ]
        }

        records = adapter._algorithm_candidate_records(
            document,
            Path("/nonexistent.pdf"),
        )

        self.assertEqual(len(records), 1)
        self.assertIn("Algorithm 2", records[0]["label"])
        self.assertIn("Parameters: learning rate", records[0]["text"])
        self.assertIn("4: x_", records[0]["text"])

    def test_algorithm_cluster_stops_after_numeric_section_and_numeric_lines_9_through_14(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "Algorithm 9 Training process",
                    "prov": [{"page_no": 1, "bbox": {"l": 30, "r": 260, "t": 760, "b": 742}}],
                },
                {
                    "label": "list_item",
                    "text": "10: input x",
                    "prov": [{"page_no": 1, "bbox": {"l": 34, "r": 258, "t": 740, "b": 724}}],
                },
                {
                    "label": "list_item",
                    "text": "11: for k = 1 to T",
                    "prov": [{"page_no": 1, "bbox": {"l": 34, "r": 258, "t": 718, "b": 702}}],
                },
                {
                    "label": "list_item",
                    "text": "12: update theta",
                    "prov": [{"page_no": 1, "bbox": {"l": 34, "r": 258, "t": 696, "b": 680}}],
                },
                {
                    "label": "list_item",
                    "text": "13: normalize parameters",
                    "prov": [{"page_no": 1, "bbox": {"l": 34, "r": 258, "t": 674, "b": 658}}],
                },
                {
                    "label": "list_item",
                    "text": "14: end for",
                    "prov": [{"page_no": 1, "bbox": {"l": 34, "r": 258, "t": 652, "b": 636}}],
                },
                {
                    "label": "text",
                    "text": "The proof follows from standard martingale arguments.",
                    "prov": [{"page_no": 1, "bbox": {"l": 34, "r": 500, "t": 620, "b": 604}}],
                },
            ]
        }

        records = adapter._algorithm_cluster_records(document["texts"], set(), 0)

        self.assertEqual(len(records), 1)
        self.assertIn("Algorithm 9", records[0]["label"])
        self.assertIn("input x", records[0]["text"])
        self.assertIn("14: end for", records[0]["text"])
        self.assertNotIn("The proof follows", records[0]["text"])

    def test_algorithm_recovery_keeps_nearby_caption_with_code_block(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Algorithm 1: Adam, our proposed algorithm for stochastic optimization.",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "code",
                    "text": "Require: alpha: Stepsize while theta not converged do return theta",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        records = adapter._algorithm_candidate_records(document, Path("/nonexistent.pdf"))
        html_text, changed = adapter._replace_algorithm_records_in_html(
            (
                "<p>Algorithm 1: Adam, our proposed algorithm for stochastic optimization.</p>"
                "<pre><code>Require: alpha: Stepsize while theta not converged do return theta</code></pre>"
            ),
            records,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(changed, 1)
        visible = adapter.HTML_TAG_RE.sub("", html_text)
        self.assertIn("Algorithm 1: Adam", visible)
        self.assertEqual(visible.count("Algorithm 1: Adam"), 1)
        self.assertIn('class="docling-algorithm-keyword">Require</strong>', html_text)
        self.assertNotIn("<p>Algorithm 1: Adam", html_text)
        self.assertNotIn("<pre><code>", html_text)

    def test_algorithm_recovery_extracts_caption_from_mixed_affiliation_node(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Jimmy Lei Ba University jimmy@example.edu "
                        "Algorithm 1: Adam, our proposed algorithm for stochastic optimization."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "code",
                    "text": "Require: alpha: Stepsize while theta not converged do return theta",
                    "prov": [{"page_no": 1}],
                },
            ]
        }

        records = adapter._algorithm_candidate_records(document, Path("/nonexistent.pdf"))

        self.assertEqual(len(records), 1)
        self.assertIn("Algorithm 1: Adam", records[0]["label"])
        self.assertNotIn("Jimmy Lei Ba", records[0]["label"])

    def test_algorithm_format_does_not_start_at_for_details_caption_phrase(self) -> None:
        formatted = adapter._format_algorithm_text(
            "Algorithm 2: AdaMax. See section 7.1 for details. "
            "Good default settings for the tested machine learning problems are alpha = 0.002. "
            "Require: alpha: Stepsize while theta not converged do return theta"
        )

        self.assertTrue(formatted.startswith("Require: alpha"))
        self.assertNotIn("for details", formatted)
        self.assertNotIn("for the tested", formatted)

    def test_algorithm_format_reconstructs_nested_indentation(self) -> None:
        formatted = adapter._format_algorithm_text(
            "for number of training iterations do "
            "for k steps do "
            "Sample minibatch of m noise samples. "
            "Update the discriminator by ascending its stochastic gradient: "
            "end for "
            "return theta"
        )

        self.assertIn("\n  for k steps do", formatted)
        self.assertIn("\n    Sample minibatch", formatted)
        self.assertIn("\n    Update the discriminator", formatted)
        self.assertIn("\n  end for", formatted)

    def test_formula_algorithm_prefers_array_text_over_fragmented_pdf_text(self) -> None:
        document = {
            "texts": [
                {
                    "label": "caption",
                    "text": "Algorithm 1: Batch Normalizing Transform",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "formula",
                    "text": (
                        r"\begin{array}{l}"
                        r"\text{Input: Values of $x$ over a mini-batch;} \\"
                        r"\text{Output:} y_i \\"
                        r"\mu_B \leftarrow \frac { 1 } { m } \sum_i x_i \quad \text{// mini-batch mean}"
                        r"\end{array}"
                    ),
                    "prov": [{"page_no": 1}],
                },
            ]
        }

        records = adapter._algorithm_candidate_records(document, Path("/nonexistent.pdf"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "docling_node_text")
        self.assertIn("Input: Values", records[0]["text"])
        self.assertIn("mini-batch mean", records[0]["text"])
        self.assertNotIn("\nm\nX\nm", records[0]["text"])

    def test_algorithm_cluster_recovery_keeps_scattered_algorithm_readable(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Algorithm 2: Training a Batch-Normalized Network",
                    "prov": [{"page_no": 1, "bbox": {"l": 40, "t": 740, "r": 260, "b": 720}}],
                },
                {
                    "label": "text",
                    "text": "Input: Network N with trainable parameters Θ",
                    "prov": [{"page_no": 1, "bbox": {"l": 40, "t": 715, "r": 260, "b": 700}}],
                },
                {
                    "label": "list_item",
                    "text": "1: N tr BN ← N // Training BN network",
                    "prov": [{"page_no": 1, "bbox": {"l": 45, "t": 695, "r": 260, "b": 680}}],
                },
                {
                    "label": "text",
                    "text": "Output: Batch-normalized network for inference",
                    "prov": [{"page_no": 1, "bbox": {"l": 40, "t": 675, "r": 260, "b": 660}}],
                },
                {
                    "label": "list_item",
                    "text": "2: for k = 1 ... K do",
                    "prov": [{"page_no": 1, "bbox": {"l": 45, "t": 655, "r": 260, "b": 640}}],
                },
            ]
        }

        records = adapter._algorithm_candidate_records(document, Path("/nonexistent.pdf"))
        html_text, changed = adapter._replace_algorithm_records_in_html(
            (
                "<p>Algorithm 2: Training a Batch-Normalized Network</p>"
                "<p>Input: Network N with trainable parameters Θ</p>"
                "<li>1: N tr BN ← N // Training BN network</li>"
                "<p>Output: Batch-normalized network for inference</p>"
                "<li>2: for k = 1 ... K do</li>"
            ),
            records,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(changed, 1)
        self.assertEqual(html_text.count("docling-algorithm-recovered"), 1)
        visible = adapter.HTML_TAG_RE.sub("", html_text)
        self.assertIn("Algorithm 2: Training a Batch-Normalized Network", visible)
        self.assertIn("Input: Network N", visible)
        self.assertIn('class="docling-algorithm-keyword">Algorithm 2</strong>', html_text)
        self.assertIn('class="docling-algorithm-keyword">Input</strong>', html_text)
        self.assertNotIn("<li>1: N tr BN", html_text)

    def test_algorithm_cluster_markdown_does_not_match_existing_code_fence(self) -> None:
        records = [
            {
                "label": "Algorithm 2: Training",
                "text": "Input: Network N\n1: N tr BN ← N",
                "html_targets": ["Input: Network N", "1: N tr BN ← N"],
            }
        ]
        fenced_blocks = [
            "```text\nInput: Network N\n1: N tr BN ← N\n```\n",
            "~~~text\nInput: Network N\n1: N tr BN ← N\n~~~\n",
            "> ~~~text\n> Input: Network N\n> 1: N tr BN ← N\n> ~~~\n",
            "- ~~~text\n  Input: Network N\n  1: N tr BN ← N\n  ~~~\n",
        ]
        for fenced in fenced_blocks:
            with self.subTest(fenced=fenced.splitlines()[0]):
                markdown = (
                    "**Algorithm block 1**\n\n"
                    + fenced
                    + "\n**Algorithm 2: Training**\n\n"
                    "Input: Network N\n\n"
                    "1: N tr BN ← N\n"
                )

                updated, changed = adapter._replace_algorithm_records_in_markdown(
                    markdown,
                    records,
                )

                self.assertEqual(changed, 1)
                self.assertIn(fenced, updated)
                self.assertIn('<pre class="docling-algorithm-block">', updated)
                self.assertIn(
                    'class="docling-algorithm-keyword">Input</strong>',
                    updated,
                )
                self.assertIn("**Algorithm block 1**", updated)
                self.assertIn("**Algorithm 2: Training**", updated)

    def test_formula_algorithm_markdown_replacement_removes_fallback_comment(self) -> None:
        records = [
            {
                "label": "Algorithm block 1",
                "text": "Input: Values\nOutput: Result",
                "formula_no": 1,
            }
        ]
        markdown = (
            "$$\n"
            "\\begin{array}{l} Input: Values \\\\ Output: Result \\end{array}\n"
            "$$\n\n"
            "<!-- formula-final-output-fallback formula=1 reason=algorithm_like_formula_array -->\n\n"
            "```text\n"
            "Algorithm 1: leaked fallback evidence\n"
            "Input: Values\n"
            "```\n"
        )

        updated, changed = adapter._replace_algorithm_records_in_markdown(markdown, records)

        self.assertEqual(changed, 1)
        self.assertIn('<pre class="docling-algorithm-block">', updated)
        self.assertIn('class="docling-algorithm-keyword">Input</strong>', updated)
        self.assertNotIn("formula-final-output-fallback", updated)
        self.assertNotIn("leaked fallback evidence", updated)

    def test_formula_algorithm_markdown_replaces_collapsed_fallback_block(self) -> None:
        records = [
            {
                "label": "Algorithm block 1",
                "text": "Input: Values\nOutput: Result",
                "formula_no": 10,
            }
        ]
        markdown = (
            "**Formula 10 fallback**: kept at its source anchor; unsafe formula output was isolated.\n"
            "<!-- formula-final-output-fallback formula=10 reason=algorithm_like_formula_array -->\n\n"
            "```text\n"
            "Algorithm 10: leaked fallback evidence\n"
            "Input: Values\n"
            "```"
        )

        updated, changed = adapter._replace_algorithm_records_in_markdown(markdown, records)

        self.assertEqual(changed, 1)
        self.assertIn('<pre class="docling-algorithm-block">', updated)
        self.assertIn('class="docling-algorithm-keyword">Output</strong>', updated)
        self.assertIn("Input: Values", adapter.HTML_TAG_RE.sub("", updated))
        self.assertNotIn("Formula 10 fallback", updated)
        self.assertNotIn("leaked fallback evidence", updated)

    def test_algorithm_recovery_ignores_literal_fallback_inside_fence(self) -> None:
        record = {
            "label": "Algorithm block 1",
            "text": "Input: Values\nOutput: Result",
            "formula_no": 1,
        }
        for opening, closing in (("```text", "```"), ("~~~text", "~~~")):
            with self.subTest(opening=opening):
                markdown = (
                    f"{opening}\n"
                    "**Formula 1 fallback**: literal example\n"
                    "<!-- formula-final-output-fallback formula=1 reason=literal -->\n"
                    f"{closing}\n"
                )

                updated, changed = adapter._replace_algorithm_records_in_markdown(
                    markdown,
                    [record],
                )

                self.assertEqual(0, changed)
                self.assertEqual(markdown, updated)

    def test_formula_algorithm_markdown_prefers_formula_comment_anchor_over_global_math_index(self) -> None:
        records = [
            {
                "label": "Algorithm block 1",
                "text": "Input: Values\nOutput: Result",
                "formula_no": 10,
            }
        ]
        markdown = (
            "$$\nz = g(Wu + b)\n$$\n\n"
            "$$\nInput: Values \\\\ Output: Result\n$$\n"
            "<!-- formula-final-output-fallback formula=10 reason=algorithm_like_formula_array -->\n\n"
            "$$\nother = formula\n$$\n"
        )

        updated, changed = adapter._replace_algorithm_records_in_markdown(markdown, records)

        self.assertEqual(changed, 1)
        self.assertIn("$$\nz = g(Wu + b)\n$$", updated)
        self.assertIn("$$\nother = formula\n$$", updated)
        self.assertIn("**Algorithm block 1**", updated)
        self.assertNotIn("formula=10", updated)

    def test_formula_algorithm_markdown_removes_orphan_math_fence_before_algorithm(self) -> None:
        records = [
            {
                "label": "Algorithm block 1",
                "text": "Input: Values\nOutput: Result",
                "formula_no": 3,
            }
        ]
        markdown = (
            "Body before algorithm.\n\n"
            "$$\n\n"
            "$$\nInput: Values \\\\ Output: Result\n$$\n"
            "<!-- formula-final-output-fallback formula=3 reason=algorithm_like_formula_array -->"
        )

        updated, changed = adapter._replace_algorithm_records_in_markdown(markdown, records)

        self.assertEqual(changed, 1)
        self.assertNotIn("$$\n\n**Algorithm", updated)
        self.assertIn("**Algorithm block 1**", updated)

    def test_formula_fallback_markdown_is_readable_not_raw_math_block(self) -> None:
        rendered = adapter._render_formula_fallback_markdown(
            {
                "formula_no": 3,
                "fallback_reason": "latex_unclosed_brace,garbled_letter_spaced_text",
                "route_a_text": r"\begin{array}{r}{g a r b l e d",
            }
        )

        self.assertIn("**Formula 3 fallback**", rendered)
        self.assertIn("formula-final-output-fallback", rendered)
        self.assertNotIn("$$", rendered)
        self.assertNotIn(r"\begin{array}", rendered)

    def test_collapse_markdown_formula_fallbacks_removes_raw_bad_tex(self) -> None:
        markdown = (
            "Before\n\n"
            r"$$\begin{array}{r}{g a r b l e d}$$"
            "\n<!-- formula-final-output-fallback formula=3 reason=latex_unclosed_brace -->\n"
            "After"
        )

        updated, count = adapter._collapse_markdown_formula_fallbacks(markdown)

        self.assertEqual(count, 1)
        self.assertIn("**Formula 3 fallback**", updated)
        self.assertNotIn(r"\begin{array}", updated)
        self.assertNotIn("$$", updated)

    def test_visual_axis_tail_is_split_from_figure_caption(self) -> None:
        caption = (
            "Figure 2 shows the frame classification error rate on the core test set. "
            "The neural net has four fully-connected hidden layers Classification Error %"
        )
        html_text, count = adapter._quarantine_visual_axis_tail_html(f"<p>{caption}</p>")

        self.assertEqual(count, 1)
        self.assertIn("kind=visual_annotation", html_text)
        self.assertNotIn("Classification Error %</p>", html_text)


class SourceCropRecoveryAndDisclosureTests(unittest.TestCase):
    def test_table_crop_clamp_separates_side_by_side_tables_and_picture(self) -> None:
        def node(label: str, left: float, right: float) -> dict[str, object]:
            return {
                "label": label,
                "prov": [{
                    "page_no": 2,
                    "bbox": {
                        "l": left,
                        "r": right,
                        "t": 100,
                        "b": 60,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }],
            }

        first = node("table", 10, 30)
        second = node("table", 40, 60)
        picture = node("picture", 70, 90)
        first_clamp = adapter._table_crop_clamp(first, [first, second], [picture])
        second_clamp = adapter._table_crop_clamp(second, [first, second], [picture])
        self.assertIsNotNone(first_clamp)
        self.assertIsNotNone(second_clamp)
        self.assertEqual(35, first_clamp["r"])
        self.assertEqual(35, second_clamp["l"])
        self.assertEqual(65, second_clamp["r"])

    def test_table_crop_clamp_topleft_uses_opposing_vertical_edges(self) -> None:
        current = {
            "label": "table",
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 20,
                    "r": 80,
                    "t": 60,
                    "b": 100,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        above = {
            "label": "table",
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 20,
                    "r": 80,
                    "t": 20,
                    "b": 50,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        below = {
            "label": "picture",
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 20,
                    "r": 80,
                    "t": 110,
                    "b": 140,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        clamp = adapter._table_crop_clamp(current, [current, above], [below])
        self.assertIsNotNone(clamp)
        self.assertEqual(55.0, clamp["t"])
        self.assertEqual(105.0, clamp["b"])

    def test_formula_context_bounds_stay_inside_page_column(self) -> None:
        left_formula = {
            "label": "formula",
            "prov": [{
                "page_no": 1,
                "bbox": {"l": 20, "r": 30, "t": 200, "b": 180, "coord_origin": "BOTTOMLEFT"},
            }],
        }
        right_formula = {
            "label": "formula",
            "prov": [{
                "page_no": 1,
                "bbox": {"l": 70, "r": 80, "t": 200, "b": 180, "coord_origin": "BOTTOMLEFT"},
            }],
        }
        self.assertEqual(50, adapter._formula_context_crop_bounds(left_formula, (100, 300))["r"])
        self.assertEqual(50, adapter._formula_context_crop_bounds(right_formula, (100, 300))["l"])

    def test_formula_context_bounds_preserve_topleft_page_orientation(self) -> None:
        formula = {
            "label": "formula",
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 20,
                    "r": 30,
                    "t": 80,
                    "b": 100,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        bounds = adapter._formula_context_crop_bounds(formula, (100, 300))
        self.assertIsNotNone(bounds)
        self.assertEqual((0.0, 300), (bounds["t"], bounds["b"]))

    def test_formula_context_bounds_do_not_cut_cross_midpoint_display_formula(self) -> None:
        cross_mid_formula = {
            "label": "formula",
            "prov": [{
                "page_no": 1,
                "bbox": {"l": 42, "r": 58, "t": 200, "b": 180, "coord_origin": "BOTTOMLEFT"},
            }],
        }
        near_mid_formula = {
            "label": "formula",
            "prov": [{
                "page_no": 1,
                "bbox": {"l": 47, "r": 49, "t": 200, "b": 180, "coord_origin": "BOTTOMLEFT"},
            }],
        }
        self.assertIsNone(adapter._formula_context_crop_bounds(cross_mid_formula, (100, 300)))
        self.assertIsNone(adapter._formula_context_crop_bounds(near_mid_formula, (100, 300)))

    def test_image_table_recovery_accepts_dense_grid_and_records_payload(self) -> None:
        empty = {
            "self_ref": "#/tables/0",
            "label": "table",
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
        }
        recovered = {
            "label": "table",
            "data": {
                "num_rows": 2,
                "num_cols": 2,
                "table_cells": [
                    {"start_row_offset_idx": row, "end_row_offset_idx": row + 1,
                     "start_col_offset_idx": col, "end_col_offset_idx": col + 1,
                     "text": f"{row},{col}", "bbox": {"l": 1, "r": 2, "t": 3, "b": 4}}
                    for row in range(2) for col in range(2)
                ],
            },
        }
        response = {"status": "success", "document": {"json_content": {"tables": [empty]}}}
        retry_response = {"status": "success", "document": {"json_content": {"tables": [recovered]}}}
        args = Namespace(serve_url="http://127.0.0.1:5001", timeout_seconds=120)
        metadata: dict[str, object] = {}
        status: dict[str, object] = {"ok": True, "success_class": "success", "warnings": [], "quality_signals": {}}
        with patch.object(adapter, "_render_table_recovery_crop_png", return_value=b"png"), patch.object(
            adapter, "post_json", return_value=retry_response
        ) as request:
            result = adapter.recover_image_only_tables_from_serve(
                response, Path("source.pdf"), args, metadata, status
            )
        self.assertEqual(1, result["accepted_count"])
        self.assertEqual(2, empty["data"]["num_rows"])
        self.assertNotIn("bbox", empty["data"]["table_cells"][0])
        payload = request.call_args.args[1]
        self.assertEqual(["image"], payload["options"]["from_formats"])
        self.assertTrue(payload["options"]["do_table_structure"])
        self.assertEqual([], status["warnings"])

    def test_image_table_recovery_rejects_sparse_grid_and_remote_endpoint(self) -> None:
        empty = {
            "self_ref": "#/tables/0",
            "label": "table",
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
        }
        response = {"status": "success", "document": {"json_content": {"tables": [empty]}}}
        args = Namespace(serve_url="https://example.invalid", timeout_seconds=120)
        status: dict[str, object] = {"ok": True, "success_class": "success", "warnings": [], "quality_signals": {}}
        result = adapter.recover_image_only_tables_from_serve(
            response, Path("source.pdf"), args, {}, status
        )
        self.assertEqual(1, result["attempted_count"])
        self.assertIn("image_table_recovery_remote_endpoint_disallowed", status["warnings"][0])

    def test_image_table_recovery_malformed_endpoint_fails_closed(self) -> None:
        empty = {
            "self_ref": "#/tables/0",
            "label": "table",
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
        }
        response = {"status": "success", "document": {"json_content": {"tables": [empty]}}}
        args = Namespace(serve_url="http://[bad", timeout_seconds=120)
        status: dict[str, object] = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        with patch.object(adapter, "post_json") as post:
            result = adapter.recover_image_only_tables_from_serve(
                response, Path("source.pdf"), args, {}, status
            )
        self.assertEqual(1, result["attempted_count"])
        self.assertFalse(status["ok"])
        self.assertTrue(
            any(
                "image_table_recovery_remote_endpoint_disallowed" in warning
                for warning in status["warnings"]
            )
        )
        post.assert_not_called()

    def test_image_table_recovery_preserves_valid_one_cell_semantics(self) -> None:
        table = {
            "self_ref": "#/tables/0",
            "label": "table",
            "data": {
                "num_rows": 1,
                "num_cols": 1,
                "table_cells": [{
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Total = 12",
                }],
            },
        }
        response = {"status": "success", "document": {"json_content": {"tables": [table]}}}
        args = Namespace(serve_url="https://example.invalid", timeout_seconds=120)
        status: dict[str, object] = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        with patch.object(adapter, "_render_table_recovery_crop_png") as render, patch.object(
            adapter, "post_json"
        ) as post:
            result = adapter.recover_image_only_tables_from_serve(
                response, Path("source.pdf"), args, {}, status
            )
        self.assertEqual(0, result["attempted_count"])
        self.assertEqual(0, result["accepted_count"])
        self.assertEqual([], result["rejected"])
        self.assertTrue(status["ok"])
        self.assertEqual("success", status["success_class"])
        render.assert_not_called()
        post.assert_not_called()

    def test_table_semantic_hint_requires_caption_form_not_plain_chinese_text(self) -> None:
        table = {
            "label": "table",
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 20,
                    "r": 80,
                    "t": 60,
                    "b": 100,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        plain = {
            "texts": [{
                "label": "text",
                "text": "结果表示如下",
                "prov": [{
                    "page_no": 1,
                    "bbox": {
                        "l": 20,
                        "r": 80,
                        "t": 10,
                        "b": 45,
                        "coord_origin": "TOPLEFT",
                    },
                }],
            }],
        }
        self.assertFalse(adapter._table_has_semantic_hint(table, plain))
        captioned = {
            "texts": [{
                "self_ref": "#/texts/1",
                "label": "caption",
                "text": "表 1：结果",
                "prov": [{
                    "page_no": 1,
                    "bbox": {
                        "l": 20,
                        "r": 80,
                        "t": 10,
                        "b": 45,
                        "coord_origin": "TOPLEFT",
                    },
                }],
            }],
        }
        table_with_caption = dict(table, captions=[{"$ref": "#/texts/1"}])
        self.assertTrue(adapter._table_has_semantic_hint(table_with_caption, captioned))

    def test_image_table_recovery_rejects_oversized_dimensions(self) -> None:
        oversized = {
            "status": "success",
            "document": {"json_content": {"tables": [{
                "label": "table",
                "data": {
                    "num_rows": 257,
                    "num_cols": 2,
                    "table_cells": [
                        {"start_row_offset_idx": row, "end_row_offset_idx": row + 1,
                         "start_col_offset_idx": col, "end_col_offset_idx": col + 1,
                         "text": "x"}
                        for row, col in ((0, 0), (0, 1), (1, 0), (1, 1))
                    ],
                },
            }]}}
        }
        data, diagnostics = adapter._recovered_table_data_from_response(oversized)
        self.assertIsNone(data)
        self.assertEqual("table_grid_limits_exceeded", diagnostics["reason"])

    def test_image_table_recovery_rejects_hostile_cell_offsets_before_grid_alloc(self) -> None:
        hostile = {
            "status": "success",
            "document": {
                "json_content": {
                    "tables": [{
                        "label": "table",
                        "data": {
                            "num_rows": 2,
                            "num_cols": 2,
                            "table_cells": [{
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1_000_000_000,
                                "start_col_offset_idx": 0,
                                "end_col_offset_idx": 1,
                                "text": "x",
                            }],
                        },
                    }],
                }
            },
        }
        with patch.object(adapter, "table_grid", side_effect=AssertionError("grid allocation")):
            data, diagnostics = adapter._recovered_table_data_from_response(hostile)
        self.assertIsNone(data)
        self.assertEqual("table_cell_offsets_out_of_bounds", diagnostics["reason"])

    def test_ruled_grid_ocr_fallback_recovers_nested_5x5_spans_and_positions(self) -> None:
        crop_bytes, response = _ruled_grid_nested_fixture()
        data, diagnostics = adapter._recovered_table_data_from_response(
            response,
            crop_bytes=crop_bytes,
            table_semantic_hint=True,
        )
        self.assertIsNotNone(data)
        self.assertTrue(diagnostics["accepted"])
        self.assertEqual("ruled_grid_ocr_fallback", diagnostics["recovery_mode"])
        self.assertEqual("validated_ruled_grid_ocr", diagnostics["validation_mode"])
        self.assertEqual(2, diagnostics["dash_marker_count"])
        self.assertGreater(diagnostics["merged_component_count"], 0)
        self.assertEqual((5, 5), (data["num_rows"], data["num_cols"]))
        cells = {cell["text"]: cell for cell in data["table_cells"] if cell.get("text")}
        self.assertEqual((0, 1, 2, 5), (
            cells["To"]["start_row_offset_idx"],
            cells["To"]["end_row_offset_idx"],
            cells["To"]["start_col_offset_idx"],
            cells["To"]["end_col_offset_idx"],
        ))
        self.assertEqual((2, 5, 0, 1), (
            cells["From"]["start_row_offset_idx"],
            cells["From"]["end_row_offset_idx"],
            cells["From"]["start_col_offset_idx"],
            cells["From"]["end_col_offset_idx"],
        ))
        expected_positions = {
            "Solid trans": (2, 2),
            "Melting": (2, 3),
            "Sublimation": (2, 4),
            "Freezing": (3, 2),
            "Boiling": (3, 4),
            "Condensation": (4, 3),
        }
        for text, (row, col) in expected_positions.items():
            self.assertEqual(
                (row, row + 1, col, col + 1),
                (
                    cells[text]["start_row_offset_idx"],
                    cells[text]["end_row_offset_idx"],
                    cells[text]["start_col_offset_idx"],
                    cells[text]["end_col_offset_idx"],
                ),
            )
        dash_cells = [cell for cell in data["table_cells"] if cell.get("text") == "-"]
        self.assertEqual(
            {(3, 3), (4, 4)},
            {
                (cell["start_row_offset_idx"], cell["start_col_offset_idx"])
                for cell in dash_cells
            },
        )
        self.assertTrue(all("bbox" not in cell for cell in data["table_cells"]))

    def test_unique_empty_table_candidate_uses_ruled_grid_retry(self) -> None:
        crop_bytes, response = _ruled_grid_nested_fixture()
        empty_candidate = {
            "label": "table",
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
        }
        retry_json = dict(response["document"]["json_content"])
        retry_json["tables"] = [empty_candidate]
        retry_response = {
            "status": "success",
            "document": {"json_content": retry_json},
        }
        data, diagnostics = adapter._recovered_table_data_from_response(
            retry_response,
            crop_bytes=crop_bytes,
            table_semantic_hint=True,
        )
        self.assertIsNotNone(data)
        self.assertEqual("validated_ruled_grid_ocr", diagnostics["validation_mode"])
        self.assertEqual("table_grid_not_dense_enough", diagnostics["structured_rejection_reason"])

    def test_ruled_grid_rejects_chart_like_sparse_labels(self) -> None:
        crop_bytes, response = _ruled_grid_sparse_chart_fixture()
        data, diagnostics = adapter._recovered_table_data_from_response(
            response,
            crop_bytes=crop_bytes,
            table_semantic_hint=False,
        )
        self.assertIsNone(data)
        self.assertEqual("ruled_grid_table_semantic_hint_missing", diagnostics["reason"])

    def test_ruled_grid_rejects_fully_labelled_non_nested_chart(self) -> None:
        crop_bytes, response = _ruled_grid_full_chart_fixture()
        data, diagnostics = adapter._recovered_table_data_from_response(
            response,
            crop_bytes=crop_bytes,
            table_semantic_hint=False,
        )
        self.assertIsNone(data)
        self.assertEqual("ruled_grid_table_semantic_hint_missing", diagnostics["reason"])
        hinted_data, hinted_diagnostics = adapter._recovered_table_data_from_response(
            response,
            crop_bytes=crop_bytes,
            table_semantic_hint=True,
        )
        self.assertIsNotNone(hinted_data)
        self.assertEqual((3, 3), (hinted_data["num_rows"], hinted_data["num_cols"]))
        self.assertEqual(0, hinted_diagnostics["merged_component_count"])

    def test_ruled_grid_ocr_fallback_rejects_no_line_and_ambiguous_ocr(self) -> None:
        crop_bytes, response = _ruled_grid_nested_fixture()
        from PIL import Image

        with Image.open(io.BytesIO(crop_bytes)).convert("L") as fixture_image:
            self.assertFalse(
                adapter._ruled_grid_dash_marker(
                    fixture_image,
                    left=10,
                    top=10,
                    right=110,
                    bottom=110,
                )
            )
            self.assertFalse(
                adapter._ruled_grid_dash_marker(
                    fixture_image,
                    left=210,
                    top=210,
                    right=310,
                    bottom=310,
                )
            )

        blank = io.BytesIO()
        Image.new("RGB", (500, 500), "white").save(blank, format="PNG")
        no_line_data, no_line_diagnostics = adapter._recovered_table_data_from_response(
            response,
            crop_bytes=blank.getvalue(),
            table_semantic_hint=True,
        )
        self.assertIsNone(no_line_data)
        self.assertEqual("ruled_grid_geometry_not_strong", no_line_diagnostics["reason"])

        ambiguous_response = {
            "status": "success",
            "document": {
                "json_content": {
                    "pages": {"1": {"size": {"width": 500, "height": 500}}},
                    "texts": [
                        {
                            "label": "text",
                            "text": f"label-{index}",
                            "prov": [{
                                "page_no": 1,
                                "bbox": {
                                    "l": 120 + index,
                                    "r": 140 + index,
                                    "t": 230 + index,
                                    "b": 240 + index,
                                    "coord_origin": "TOPLEFT",
                                },
                            }],
                        }
                        for index in range(4)
                    ],
                }
            },
        }
        ambiguous_data, ambiguous_diagnostics = adapter._recovered_table_data_from_response(
            ambiguous_response,
            crop_bytes=crop_bytes,
            table_semantic_hint=True,
        )
        self.assertIsNone(ambiguous_data)
        self.assertEqual(
            "ruled_grid_cell_coverage_insufficient",
            ambiguous_diagnostics["reason"],
        )

    def test_image_table_recovery_passes_crop_bytes_to_ruled_grid_verifier(self) -> None:
        crop_bytes, retry_response = _ruled_grid_nested_fixture()
        empty = {
            "self_ref": "#/tables/0",
            "label": "table",
            "captions": [{"$ref": "#/texts/0"}],
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
        }
        source_response = {
            "status": "success",
            "document": {
                "json_content": {
                    "tables": [empty],
                    "texts": [{
                        "self_ref": "#/texts/0",
                        "label": "caption",
                        "text": "Nested table",
                        "prov": [{"page_no": 1}],
                    }],
                }
            },
        }
        args = Namespace(serve_url="http://127.0.0.1:5001", timeout_seconds=120)
        status: dict[str, object] = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        with patch.object(adapter, "_render_table_recovery_crop_png", return_value=crop_bytes), patch.object(
            adapter, "post_json", return_value=retry_response
        ):
            result = adapter.recover_image_only_tables_from_serve(
                source_response,
                Path("source.pdf"),
                args,
                {},
                status,
            )
        self.assertEqual(1, result["accepted_count"])
        self.assertEqual(
            "ruled_grid_ocr_fallback",
            empty["local_ai_lab_qc"]["image_table_semantic_recovery"]["recovery_mode"],
        )
        self.assertEqual(5, empty["data"]["num_rows"])
        self.assertEqual(5, empty["data"]["num_cols"])

    def test_image_table_recovery_uses_local_ruled_grid_before_backend_endpoint(self) -> None:
        crop_bytes, ocr_response = _ruled_grid_nested_fixture()
        empty = {
            "self_ref": "#/tables/0",
            "label": "table",
            "captions": [{"$ref": "#/texts/99"}],
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 0,
                    "r": 500,
                    "t": 0,
                    "b": 500,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        source_json = copy.deepcopy(ocr_response["document"]["json_content"])
        # Keep an unrelated semantic table in the source response.  The
        # local-first ruled verifier must consume only mapped OCR for the
        # target crop rather than reject this response as non-unique.
        source_json["tables"] = [
            empty,
            {
                "self_ref": "#/tables/other",
                "label": "table",
                "data": {
                    "num_rows": 1,
                    "num_cols": 1,
                    "table_cells": [{
                        "start_row_offset_idx": 0,
                        "end_row_offset_idx": 1,
                        "start_col_offset_idx": 0,
                        "end_col_offset_idx": 1,
                        "text": "unrelated",
                    }],
                },
            },
        ]
        # Same-coordinate OCR from another page must not be allowed to fill
        # the crop or inflate its recovered evidence count.
        other_page_texts = copy.deepcopy(source_json.get("texts", []))
        for node in other_page_texts:
            for prov in node.get("prov") or []:
                if isinstance(prov, dict):
                    prov["page_no"] = 2
            node["text"] = f"other-{node.get('text', '')}"
        source_json.setdefault("texts", []).extend(other_page_texts)
        source_json.setdefault("texts", []).append({
            "self_ref": "#/texts/99",
            "label": "caption",
            "text": "Nested table",
            "prov": [{"page_no": 1}],
        })
        source_response = {
            "status": "success",
            "document": {"json_content": source_json},
        }
        geometry = {
            "page_no": 1,
            "page_size": {"width": 500, "height": 500},
            "page_image_size": {"width": 500, "height": 500},
            "origin": "TOPLEFT",
            "pixel_box": [0, 0, 500, 500],
            "render_scale": 1.0,
        }
        args = Namespace(serve_url="http://backend:5001", timeout_seconds=120)
        status: dict[str, object] = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        with patch.object(
            adapter,
            "_render_table_recovery_crop_png_with_geometry",
            return_value=(crop_bytes, geometry),
        ), patch.object(adapter, "post_json") as post:
            result = adapter.recover_image_only_tables_from_serve(
                source_response,
                Path("source.pdf"),
                args,
                {},
                status,
            )
        self.assertEqual(1, result["accepted_count"])
        self.assertEqual(5, empty["data"]["num_rows"])
        self.assertEqual(
            "local_crop",
            empty["local_ai_lab_qc"]["image_table_semantic_recovery"]["recovery_origin"],
        )
        self.assertEqual(
            15,
            empty["local_ai_lab_qc"]["image_table_semantic_recovery"]
            ["local_recovery"]["ocr_nonempty_count"],
        )
        self.assertTrue(status["ok"])
        post.assert_not_called()

    def test_image_table_recovery_rejects_same_page_overlapping_table_ocr(self) -> None:
        crop_bytes, ocr_response = _ruled_grid_nested_fixture()
        empty = {
            "self_ref": "#/tables/0",
            "label": "table",
            "captions": [{"$ref": "#/texts/99"}],
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 0,
                    "r": 500,
                    "t": 0,
                    "b": 500,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        overlapping_table = {
            "self_ref": "#/tables/other",
            "label": "table",
            "data": {
                "num_rows": 1,
                "num_cols": 1,
                "table_cells": [{
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "OTHER_TABLE",
                }],
            },
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 230,
                    "r": 290,
                    "t": 230,
                    "b": 290,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }
        source_json = copy.deepcopy(ocr_response["document"]["json_content"])
        source_json["tables"] = [empty, overlapping_table]
        source_json.setdefault("texts", []).extend([
            {
                "self_ref": "#/texts/99",
                "label": "caption",
                "text": "Nested table",
                "prov": [{"page_no": 1}],
            },
            {
                "self_ref": "#/texts/other",
                "label": "text",
                "text": "OTHER_TABLE",
                "prov": [{
                    "page_no": 1,
                    "bbox": {
                        "l": 245,
                        "r": 280,
                        "t": 250,
                        "b": 270,
                        "coord_origin": "TOPLEFT",
                    },
                }],
            },
        ])
        source_response = {
            "status": "success",
            "document": {"json_content": source_json},
        }
        geometry = {
            "page_no": 1,
            "page_size": {"width": 500, "height": 500},
            "page_image_size": {"width": 500, "height": 500},
            "origin": "TOPLEFT",
            "pixel_box": [0, 0, 500, 500],
            "render_scale": 1.0,
        }
        args = Namespace(serve_url="http://backend:5001", timeout_seconds=120)
        status: dict[str, object] = {
            "ok": True,
            "success_class": "success",
            "warnings": [],
            "quality_signals": {},
        }
        with patch.object(
            adapter,
            "_render_table_recovery_crop_png_with_geometry",
            return_value=(crop_bytes, geometry),
        ), patch.object(adapter, "post_json") as post:
            result = adapter.recover_image_only_tables_from_serve(
                source_response,
                Path("source.pdf"),
                args,
                {},
                status,
            )

        self.assertEqual(0, result["accepted_count"])
        self.assertEqual(1, len(result["rejected"]))
        self.assertEqual(
            "local_crop_overlaps_non_target_table",
            result["rejected"][0]["reason"],
        )
        self.assertEqual(
            "local_crop_overlaps_non_target_table",
            result["rejected"][0]["local_recovery"]["reason"],
        )
        self.assertEqual(0, empty["data"]["num_rows"])
        self.assertFalse(status["ok"])
        post.assert_not_called()

    def test_source_disclosures_are_closed_styled_and_idempotent(self) -> None:
        document = {
            "tables": [{
                "self_ref": "#/tables/0",
                "label": "table",
                "data": {
                    "num_rows": 2,
                    "num_cols": 2,
                    "table_cells": [
                        {"start_row_offset_idx": row, "end_row_offset_idx": row + 1,
                         "start_col_offset_idx": col, "end_col_offset_idx": col + 1,
                         "text": f"{row},{col}"}
                        for row in range(2) for col in range(2)
                    ],
                },
                "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 50, "t": 80, "b": 40, "coord_origin": "BOTTOMLEFT"}}],
            }]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "tables").mkdir()
            (output_dir / "tables" / "table_1.png").write_bytes(b"png")
            (output_dir / "document.html").write_text(
                '<html><head></head><body><figure class="semantic-table"><table data-source-ref="#/tables/0"><tr><td>0,0</td></tr></table></figure></body></html>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("<!-- source-table-ref:#/tables/0 -->\n", encoding="utf-8")
            adapter.append_structured_table_source_renderings(output_dir, document, document["tables"])
            adapter.append_structured_table_source_renderings(output_dir, document, document["tables"])
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")
        self.assertEqual(1, html_text.count("docling-table-source-disclosure"))
        self.assertEqual(1, html_text.count("docling-table-source-evidence"))
        self.assertEqual(1, html_text.count("tables/table_1.png"))
        self.assertEqual(1, markdown_text.count("docling-table-source-disclosure"))
        self.assertEqual(1, markdown_text.count("tables/table_1.png"))
        self.assertNotIn("<details open", html_text)
        styled, _ = adapter._inject_english_review_style(html_text)
        self.assertIn("@media print", styled)
        self.assertIn("docling-source-disclosure", styled)

    def test_empty_table_visual_fallback_is_idempotent_on_html_and_markdown(self) -> None:
        table = {
            "self_ref": "#/tables/0",
            "label": "table",
            "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
            "prov": [{
                "page_no": 1,
                "bbox": {"l": 10, "r": 50, "t": 80, "b": 40, "coord_origin": "BOTTOMLEFT"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "tables").mkdir()
            _write_visible_test_png(output_dir / "tables" / "table_1.png")
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                '<table data-source-ref="#/tables/0"></table>'
                "</figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "<!-- source-table-ref:#/tables/0 -->\n\n"
                "| |\n|---|\n",
                encoding="utf-8",
            )
            adapter.inject_empty_table_visual_fallbacks(
                output_dir,
                {"tables": [table]},
                [table],
            )
            adapter.inject_empty_table_visual_fallbacks(
                output_dir,
                {"tables": [table]},
                [table],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")
        self.assertEqual(1, html_text.count("docling-table-visual-fallback"))
        self.assertEqual(1, html_text.count("tables/table_1.png"))
        self.assertEqual(1, markdown.count("source-empty-table-ref:#/tables/0"))
        self.assertEqual(1, markdown.count("tables/table_1.png"))

    def test_markdown_source_image_escapes_caption_and_rejects_remote_path(self) -> None:
        rendered = adapter._markdown_image(
            "x](https://evil.example/evil.png)\n# forged heading",
            "tables/table_1.png",
        )
        self.assertIn(r"x\]\(https://evil.example/evil.png\)", rendered)
        self.assertNotIn("](https://evil.example/evil.png)", rendered)
        self.assertNotIn("\n# forged heading", rendered)
        self.assertEqual(
            "![caption](#)",
            adapter._markdown_image("caption", "https://evil.example/crop.png"),
        )

    def test_final_restore_reinjects_disclosure_style_after_evidence_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                '<html><head></head><body><details class="docling-source-disclosure"><summary>Source</summary><p>crop</p></details></body></html>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("Source\n", encoding="utf-8")
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {"primary_surface": {"counts": {}}},
            }
            adapter.restore_final_delivery_visuals(
                output_dir,
                {},
                Path("missing.pdf"),
                metadata,
                status,
                visual_pdf_path=Path("missing.pdf"),
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
        self.assertIn("docling-english-review-polish-style", html_text)
        self.assertIn("@media print", html_text)
        self.assertNotIn("<details open", html_text)

    def test_stale_review_style_is_replaced_without_duplicate_style_ids(self) -> None:
        stale = (
            '<html><head><style id="docling-english-review-polish-style">'
            ".old-style { color: red; }"
            "</style></head><body>Body</body></html>"
        )
        updated, changed = adapter._inject_english_review_style(stale)
        self.assertTrue(changed)
        self.assertEqual(1, updated.count('id="docling-english-review-polish-style"'))
        self.assertIn("docling-source-disclosure", updated)
        self.assertNotIn("old-style", updated)

    def test_formula_number_diagnostics_share_ocr_spaced_number(self) -> None:
        self.assertEqual(12, adapter._formula_number_match_value(adapter.FORMULA_NUMBER_RE.search("( 1 2 )")))
        self.assertTrue(adapter._is_formula_number_only("( 1 2 )"))
        diagnostics = adapter.formula_number_qc_diagnostics(
            [{"text": "x+y ( 1 2 )", "prov": {}}],
            "<html><body></body></html>",
        )
        self.assertEqual(12, diagnostics[0]["recovered_number"])
        self.assertTrue(diagnostics[0]["safe_to_recover"])


if __name__ == "__main__":
    unittest.main()
