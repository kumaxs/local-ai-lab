from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import types

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vlm_full_dir_review as vlm_review  # noqa: E402


@contextlib.contextmanager
def fake_docling_runtime(document_markdown: str = "# doc", formula_count: int = 1) -> Iterator[None]:
    class _FakeImage:
        def save(self, path: os.PathLike[str] | str) -> None:
            Path(path).write_bytes(b"\x89PNG\r\n\ndummy")

    class _FakePage:
        image = SimpleNamespace(pil_image=_FakeImage())

    class _FakeDoc:
        def __init__(self) -> None:
            self.pages = {1: _FakePage()}

        def save_as_markdown(self, path: Path, **_kwargs: object) -> None:
            path.write_text(document_markdown, encoding="utf-8")

        def save_as_html(self, path: Path, **_kwargs: object) -> None:
            path.write_text("<p>doc</p>", encoding="utf-8")

        def save_as_json(self, path: Path, **_kwargs: object) -> None:
            path.write_text(json.dumps({"texts": [{"label": "formula", "text": "x"}] * formula_count}), encoding="utf-8")

        def export_to_dict(self) -> dict[str, Any]:
            return {"texts": [{"label": "formula", "text": "x"}] * formula_count}

    class _FakeResult:
        def __init__(self) -> None:
            self.document = _FakeDoc()

    class _BaseModels:
        PDF = "PDF"

    class _VlmConvertOptions:
        @classmethod
        def from_preset(cls, *_args: Any, **_kwargs: Any) -> object:
            return cls()

    class _VlmPipelineOptions:
        def __init__(self, *args: Any, **_kwargs: Any) -> None:
            self.args = args
            self.kwargs = _kwargs

    class _MlxVlmEngineOptions:
        pass

    class _PdfFormatOption:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

    class _VlmPipeline:
        pass

    class _DocumentConverter:
        def __init__(self, *args: Any, **_kwargs: Any) -> None:
            self.args = args
            self.kwargs = _kwargs

        def convert(self, _pdf: Path) -> _FakeResult:
            return _FakeResult()

    class _ImageRefMode:
        REFERENCED = "referenced"

    docling = types.ModuleType("docling")
    datamodel = types.ModuleType("docling.datamodel")
    base_models = types.ModuleType("docling.datamodel.base_models")
    pipeline_options = types.ModuleType("docling.datamodel.pipeline_options")
    vlm_engine_options = types.ModuleType("docling.datamodel.vlm_engine_options")
    document_converter = types.ModuleType("docling.document_converter")
    pipeline = types.ModuleType("docling.pipeline")
    vlm_pipeline = types.ModuleType("docling.pipeline.vlm_pipeline")
    docling_core = types.ModuleType("docling_core")
    types_mod = types.ModuleType("docling_core.types")
    doc_mod = types.ModuleType("docling_core.types.doc")

    docling.datamodel = datamodel
    datamodel.base_models = base_models
    datamodel.pipeline_options = pipeline_options
    datamodel.vlm_engine_options = vlm_engine_options

    base_models.InputFormat = _BaseModels
    pipeline_options.VlmConvertOptions = _VlmConvertOptions
    pipeline_options.VlmPipelineOptions = _VlmPipelineOptions
    vlm_engine_options.MlxVlmEngineOptions = _MlxVlmEngineOptions
    document_converter.DocumentConverter = _DocumentConverter
    document_converter.PdfFormatOption = _PdfFormatOption

    pipeline.vlm_pipeline = vlm_pipeline
    vlm_pipeline.VlmPipeline = _VlmPipeline
    types_mod.doc = doc_mod
    doc_mod.ImageRefMode = _ImageRefMode

    fake_modules = {
        "docling": docling,
        "docling.datamodel": datamodel,
        "docling.datamodel.base_models": base_models,
        "docling.datamodel.pipeline_options": pipeline_options,
        "docling.datamodel.vlm_engine_options": vlm_engine_options,
        "docling.document_converter": document_converter,
        "docling.pipeline": pipeline,
        "docling.pipeline.vlm_pipeline": vlm_pipeline,
        "docling_core": docling_core,
        "docling_core.types": types_mod,
        "docling_core.types.doc": doc_mod,
    }
    with patch.dict(sys.modules, fake_modules, clear=False):
        yield


class VlmFullDirReviewMetadataTests(unittest.TestCase):
    def test_success_run_worker_includes_input_sha256_and_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "document.pdf"
            sample.write_bytes(b"route-b-source")
            expected_sha256 = hashlib.sha256(b"route-b-source").hexdigest()
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=Path(directory),
                artifacts_path=Path(directory),
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / args.worker_job_id
            reference_file = output_dir / "source.pdf"
            with (
                fake_docling_runtime(),
                patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ),
            ):
                return_code = vlm_review.run_worker(args)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(return_code, 0)
            self.assertEqual(metadata.get("input_file"), str(sample))
            self.assertEqual(metadata.get("input_sha256"), expected_sha256)
            self.assertIn(
                metadata.get("input_file_reference_mode"),
                {"copied", "existing"},
            )
            self.assertEqual(metadata.get("input_file_reference"), str(reference_file))
            self.assertTrue(metadata.get("input_file_reference_verified"))
            self.assertEqual(reference_file.exists(), True)

    def test_model_unavailable_failure_run_worker_includes_input_sha256_and_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "document.pdf"
            sample.write_bytes(b"route-b-failure-source")
            expected_sha256 = hashlib.sha256(b"route-b-failure-source").hexdigest()
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=Path(directory),
                artifacts_path=Path(directory),
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / args.worker_job_id
            with patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", ["missing"], False),
            ):
                return_code = vlm_review.run_worker(args)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(return_code, 1)
            self.assertEqual(metadata.get("input_file"), str(sample))
            self.assertEqual(metadata.get("input_sha256"), expected_sha256)
            self.assertEqual(status.get("success_class"), "failure")
            self.assertEqual(metadata.get("input_file_reference"), str(output_dir / "source.pdf"))

    def test_existing_source_pdf_sha_mismatch_fails_with_identity_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "document.pdf"
            stale_source = Path(directory) / "stale-source.pdf"
            sample.write_bytes(b"route-b-new")
            stale_source.write_bytes(b"route-b-old")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=Path(directory),
                artifacts_path=Path(directory),
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / args.worker_job_id
            output_dir.mkdir()
            (output_dir / "source.pdf").write_bytes(stale_source.read_bytes())
            with patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", [], True),
            ):
                return_code = vlm_review.run_worker(args)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))

            self.assertEqual(return_code, 1)
            self.assertEqual(status.get("success_class"), "failure")
            self.assertFalse(metadata.get("input_file_reference_verified"))
            self.assertEqual(metadata.get("input_file_reference_mode"), "existing_mismatch")
            self.assertIn("source_reference_sha_mismatch", metadata.get("input_file_reference_error", ""))

    def test_existing_matching_source_pdf_is_verified_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "document.pdf"
            sample.write_bytes(b"route-b-stable")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=Path(directory),
                artifacts_path=Path(directory),
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / args.worker_job_id
            output_dir.mkdir()
            existing_source = output_dir / "source.pdf"
            os.link(str(sample), str(existing_source))
            with (
                fake_docling_runtime(),
                patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ),
            ):
                return_code = vlm_review.run_worker(args)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(return_code, 0)
            self.assertTrue(metadata.get("input_file_reference_verified"))
            self.assertEqual(metadata.get("input_file_reference_mode"), "existing")
            self.assertEqual(metadata.get("input_file_reference"), str(existing_source))

    def test_model_unavailable_failure_removes_stale_contract_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "document.pdf"
            sample.write_bytes(b"route-b-model-missing")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=Path(directory),
                artifacts_path=Path(directory),
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / args.worker_job_id
            output_dir.mkdir()
            source_path = output_dir / "source.pdf"
            os.link(str(sample), str(source_path))
            stale_json = output_dir / "document.json"
            stale_md = output_dir / "document.md"
            stale_html = output_dir / "document.html"
            stale_json.write_text("{\"legacy\": true}", encoding="utf-8")
            stale_md.write_text("legacy", encoding="utf-8")
            stale_html.write_text("<p>legacy</p>", encoding="utf-8")

            with patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", ["missing"], False),
            ):
                return_code = vlm_review.run_worker(args)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))

            self.assertEqual(return_code, 1)
            self.assertEqual(status.get("success_class"), "failure")
            self.assertEqual(metadata.get("input_file_reference_mode"), "existing")
            self.assertTrue((output_dir / "source.pdf").exists())
            self.assertFalse((output_dir / "document.json").exists())
            self.assertFalse((output_dir / "document.md").exists())
            self.assertFalse((output_dir / "document.html").exists())

    def test_retry_quarantines_all_old_assets_before_success(self) -> None:
        """A retry must not mix a prior failed run's pages/tables/artifacts."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"route-b-retry-source")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = root / "sample"
            output_dir.mkdir()
            os.link(str(sample), str(output_dir / "source.pdf"))
            (output_dir / "metadata.json").write_text('{"old": true}', encoding="utf-8")
            (output_dir / "status.json").write_text('{"ok": false}', encoding="utf-8")
            (output_dir / "document.json").write_text('{"old": true}', encoding="utf-8")
            (output_dir / "document.md").write_text("old markdown", encoding="utf-8")
            (output_dir / "document.html").write_text("<p>old</p>", encoding="utf-8")
            (output_dir / "pages").mkdir()
            (output_dir / "pages" / "page_99.png").write_bytes(b"old-page")
            (output_dir / "tables").mkdir()
            (output_dir / "tables" / "table_99.json").write_text("{}", encoding="utf-8")
            (output_dir / "artifacts").mkdir()
            (output_dir / "artifacts" / "old.bin").write_bytes(b"old-artifact")

            with patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", ["missing"], False),
            ):
                self.assertEqual(vlm_review.run_worker(args), 1)
            self.assertFalse((output_dir / "document.json").exists())
            self.assertFalse((output_dir / "pages" / "page_99.png").exists())
            self.assertFalse((output_dir / "tables" / "table_99.json").exists())
            quarantine_dirs = sorted(root.glob(".sample.vlm_quarantine_*"))
            self.assertTrue(quarantine_dirs)
            self.assertTrue((quarantine_dirs[-1] / "pages" / "page_99.png").exists())

            with (
                fake_docling_runtime(),
                patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ),
            ):
                self.assertEqual(vlm_review.run_worker(args), 0)
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertTrue(status.get("ok"))
            self.assertFalse((output_dir / "pages" / "page_99.png").exists())
            self.assertTrue((output_dir / "pages" / "page_1.png").exists())
            self.assertEqual(_sha256(output_dir / "source.pdf"), _sha256(sample))

    def test_quarantine_readme_symlink_cannot_overwrite_external_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "external-sentinel.txt"
            sentinel.write_text("do not overwrite", encoding="utf-8")
            output_dir = root / "sample"
            output_dir.mkdir()
            os.symlink(sentinel, output_dir / vlm_review.QUARANTINE_README_NAME)

            quarantine_path = vlm_review._quarantine_stale_output_dir(output_dir)

            self.assertIsNotNone(quarantine_path)
            assert quarantine_path is not None
            readme = quarantine_path / vlm_review.QUARANTINE_README_NAME
            self.assertFalse(readme.is_symlink())
            self.assertTrue(readme.is_file())
            self.assertIn("stale or failed VLM review output", readme.read_text(encoding="utf-8"))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite")

    def test_quarantine_marker_existing_regular_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "sample"
            output_dir.mkdir()
            (output_dir / vlm_review.QUARANTINE_README_NAME).write_text(
                "user-owned marker",
                encoding="utf-8",
            )

            with self.assertRaises(FileExistsError):
                vlm_review._quarantine_stale_output_dir(output_dir)

    def test_quarantine_retention_is_bounded_and_never_follows_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external_tree = root / "external-tree"
            external_tree.mkdir()
            sentinel = external_tree / "sentinel.txt"
            sentinel.write_text("keep me", encoding="utf-8")
            old_symlink = root / ".sample.vlm_quarantine_old_symlink"
            os.symlink(external_tree, old_symlink, target_is_directory=True)

            output_dir = root / "sample"
            quarantine_paths: list[Path] = []
            for attempt in range(5):
                output_dir.mkdir()
                (output_dir / "attempt.txt").write_text(str(attempt), encoding="utf-8")
                quarantine_path = vlm_review._quarantine_stale_output_dir(output_dir)
                self.assertIsNotNone(quarantine_path)
                assert quarantine_path is not None
                quarantine_paths.append(quarantine_path)

            retained = sorted(root.glob(".sample.vlm_quarantine_*"))
            self.assertEqual(len(retained), vlm_review.QUARANTINE_RETENTION_COUNT)
            self.assertTrue(all(path.is_dir() and not path.is_symlink() for path in retained))
            self.assertTrue(all((path / vlm_review.QUARANTINE_README_NAME).is_file() for path in retained))
            self.assertFalse(old_symlink.exists())
            self.assertFalse(old_symlink.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")

            # The active output name is not a quarantine sibling and must not
            # be removed by retention maintenance.
            output_dir.mkdir()
            active_marker = output_dir / "current.txt"
            active_marker.write_text("live", encoding="utf-8")
            vlm_review._prune_quarantine_retention(output_dir)
            self.assertEqual(active_marker.read_text(encoding="utf-8"), "live")

    def test_publish_staging_uses_exclusive_per_output_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "sample"
            staging_dir = root / ".sample.vlm_staging_test"
            staging_dir.mkdir()
            (staging_dir / "status.json").write_text("{}", encoding="utf-8")
            lock_path = root / f".{output_dir.name}{vlm_review.PUBLISH_LOCK_SUFFIX}"
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            with self.assertRaises(FileExistsError):
                vlm_review._publish_staging_output(staging_dir, output_dir)
            os.close(lock_fd)

            self.assertTrue(staging_dir.exists())
            self.assertFalse(output_dir.exists())
            vlm_review._publish_staging_output(staging_dir, output_dir)
            self.assertTrue((output_dir / "status.json").exists())
            self.assertTrue(lock_path.exists())

    def test_publish_staging_reuses_stale_regular_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "sample"
            staging_dir = root / ".sample.vlm_staging_reuse_lock"
            staging_dir.mkdir()
            (staging_dir / "status.json").write_text("{}", encoding="utf-8")
            lock_path = root / f".{output_dir.name}{vlm_review.PUBLISH_LOCK_SUFFIX}"
            lock_path.write_text("stale lock", encoding="utf-8")

            vlm_review._publish_staging_output(staging_dir, output_dir)
            self.assertTrue((output_dir / "status.json").exists())
            self.assertTrue(lock_path.exists())

    def test_publish_lock_symlink_is_not_touched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "sample"
            staging_dir = root / ".sample.vlm_staging_symlink_lock"
            staging_dir.mkdir()
            (staging_dir / "status.json").write_text("{}", encoding="utf-8")
            sentinel = root / "lock-target.txt"
            sentinel.write_text("target", encoding="utf-8")
            lock_path = root / f".{output_dir.name}{vlm_review.PUBLISH_LOCK_SUFFIX}"
            os.symlink(sentinel, lock_path)

            with self.assertRaises(FileExistsError):
                vlm_review._publish_staging_output(staging_dir, output_dir)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "target")

    def test_run_worker_honors_held_output_lock_without_mutating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"route-b-locked-source")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / args.worker_job_id
            lock_path = root / f".{output_dir.name}{vlm_review.PUBLISH_LOCK_SUFFIX}"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ):
                    self.assertEqual(vlm_review.run_worker(args), 1)
                self.assertFalse(output_dir.exists())
            finally:
                os.close(lock_fd)

    def test_output_locks_are_per_job_and_stale_lock_file_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"route-b-cross-job")
            locked_args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            independent_args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="independent",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            sample_lock = root / f".{locked_args.worker_job_id}{vlm_review.PUBLISH_LOCK_SUFFIX}"
            lock_fd = os.open(sample_lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ):
                    self.assertEqual(vlm_review.run_worker(locked_args), 1)

                with (
                    fake_docling_runtime(),
                    patch.object(
                        vlm_review,
                        "model_selection",
                        return_value=("granite_docling_mlx", [], True),
                    ),
                ):
                    self.assertEqual(vlm_review.run_worker(independent_args), 0)

                independent_output = root / independent_args.worker_job_id
                self.assertTrue(independent_output.exists())
                self.assertFalse((root / locked_args.worker_job_id).exists())
            finally:
                os.close(lock_fd)

            stale_lock = root / f".{independent_args.worker_job_id}{vlm_review.PUBLISH_LOCK_SUFFIX}"
            stale_lock.write_text("stale lock", encoding="utf-8")
            output_dir = root / independent_args.worker_job_id
            with fake_docling_runtime(), patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", [], True),
            ):
                self.assertEqual(vlm_review.run_worker(independent_args), 0)
            self.assertTrue(output_dir.exists())

    def test_input_symlink_is_rejected_for_source_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.pdf"
            target.write_bytes(b"route-b-target")
            symlink = root / "source-link.pdf"
            symlink.symlink_to(target)
            args = SimpleNamespace(
                worker_pdf=symlink,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            with patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", [], True),
            ):
                return_code = vlm_review.run_worker(args)

            output_dir = root / "sample"
            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(return_code, 1)
            self.assertEqual(metadata.get("input_file_reference"), str(output_dir / "source.pdf"))
            self.assertFalse(metadata.get("input_file_reference_verified"))
            self.assertEqual(status.get("success_class"), "failure")
            self.assertFalse((output_dir / "source.pdf").exists())

    def test_snapshot_source_copy_enforces_runtime_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "document.pdf"
            source.write_bytes(b"start")
            output_dir = root / "sample"
            output_dir.mkdir()
            reference = output_dir / vlm_review.SOURCE_REFERENCE_PATH
            chunks = [b"1234", b"56789", b""]
            call_count = 0

            def read_once(_fd: int, _size: int) -> bytes:
                nonlocal call_count
                value = chunks[call_count]
                call_count += 1
                return value

            with (
                patch.object(vlm_review, "MAX_SOURCE_COPY_BYTES", 8),
                patch.object(vlm_review.os, "read", side_effect=read_once),
            ):
                digest = vlm_review._snapshot_pdf_to_reference(source, reference)

            self.assertIsNone(digest)
            self.assertFalse(reference.exists())
            self.assertEqual(call_count, 2)
            tmp_candidates = sorted(output_dir.glob(".source.pdf.tmp.*"))
            self.assertEqual(tmp_candidates, [])

    def test_snapshot_tmp_preexisting_symlink_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "document.pdf"
            source.write_bytes(b"source")
            output_dir = root / "sample"
            output_dir.mkdir()
            reference = output_dir / vlm_review.SOURCE_REFERENCE_PATH
            sentinel = root / "sentinel.txt"
            sentinel.write_text("protected", encoding="utf-8")
            tmp_path = reference.with_name(".source.pdf.tmp.999.111")
            tmp_path.symlink_to(sentinel)

            with (
                patch.object(vlm_review.os, "getpid", return_value=999),
                patch.object(vlm_review.time, "time_ns", return_value=111),
            ):
                digest = vlm_review._snapshot_pdf_to_reference(source, reference)

            self.assertIsNone(digest)
            self.assertTrue(tmp_path.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "protected")
            self.assertFalse(reference.exists())

    def test_snapshot_rejects_non_regular_source_and_closes_input_fd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-dir"
            source.mkdir()
            output_dir = root / "sample"
            output_dir.mkdir()
            reference = output_dir / vlm_review.SOURCE_REFERENCE_PATH
            original_close = vlm_review.os.close
            closed: list[int] = []

            def close_spy(fd: int) -> int:
                closed.append(fd)
                return original_close(fd)

            with patch.object(vlm_review.os, "close", side_effect=close_spy):
                digest = vlm_review._snapshot_pdf_to_reference(source, reference)

            self.assertIsNone(digest)
            self.assertTrue(closed)
            self.assertEqual(len(closed), 1)
            self.assertFalse(reference.exists())

    def test_snapshot_fstat_error_closes_input_fd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "document.pdf"
            source.write_bytes(b"source")
            output_dir = root / "sample"
            output_dir.mkdir()
            reference = output_dir / vlm_review.SOURCE_REFERENCE_PATH
            original_close = vlm_review.os.close
            closed: list[int] = []

            def close_spy(fd: int) -> int:
                closed.append(fd)
                return original_close(fd)

            with (
                patch.object(vlm_review.os, "fstat", side_effect=OSError("broken")),
                patch.object(vlm_review.os, "close", side_effect=close_spy),
            ):
                digest = vlm_review._snapshot_pdf_to_reference(source, reference)

            self.assertIsNone(digest)
            self.assertTrue(closed)
            self.assertEqual(len(closed), 1)
            self.assertFalse(reference.exists())

    def test_running_source_replacement_does_not_change_staged_conversion_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"initial source")
            expected_sha256 = _sha256(sample)
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = args.output_root / "sample"
            captured: dict[str, object] = {}

            class _MutatingDoc:
                pages = {}

                def save_as_markdown(self, path: Path, **_kwargs: object) -> None:
                    path.write_text("markdown", encoding="utf-8")

                def save_as_html(self, path: Path, **_kwargs: object) -> None:
                    path.write_text("<p>html</p>", encoding="utf-8")

                def save_as_json(self, path: Path, **_kwargs: object) -> None:
                    path.write_text("{}", encoding="utf-8")

                def export_to_dict(self) -> dict[str, Any]:
                    return {"texts": []}

            class _MutatingResult:
                def __init__(self, document: Any) -> None:
                    self.document = document

            class _MutatingConverter:
                def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                    pass

                def convert(self, pdf: Path) -> Any:
                    captured["path"] = str(pdf)
                    captured["content"] = pdf.read_bytes()
                    sample.write_bytes(b"changed while running")
                    return _MutatingResult(_MutatingDoc())

            with (
                fake_docling_runtime(),
                patch.object(
                    sys.modules["docling.document_converter"],
                    "DocumentConverter",
                    _MutatingConverter,
                ),
                patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ),
            ):
                self.assertEqual(vlm_review.run_worker(args), 0)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata.get("input_sha256"), expected_sha256)
            self.assertTrue(str(captured["path"]).endswith("/source.pdf"))
            self.assertIn(".sample.vlm_staging_", str(captured["path"]))
            self.assertEqual(captured["content"], b"initial source")
            self.assertEqual((output_dir / "source.pdf").read_bytes(), b"initial source")

    def test_failed_conversion_publishes_clean_failure_then_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"route-b-conversion-retry")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )

            class _FailingConverter:
                def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                    pass

                def convert(self, _pdf: Path) -> Any:
                    raise RuntimeError("synthetic conversion failure")

            with fake_docling_runtime(), patch.object(
                sys.modules["docling.document_converter"],
                "DocumentConverter",
                _FailingConverter,
            ), patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", [], True),
            ):
                self.assertEqual(vlm_review.run_worker(args), 1)
            output_dir = root / "sample"
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(status.get("ok"))
            self.assertTrue((output_dir / "source.pdf").exists())
            for stale_name in ("document.json", "document.md", "document.html", "review_index.html"):
                self.assertFalse((output_dir / stale_name).exists())
            self.assertFalse((output_dir / "pages").exists())
            self.assertFalse((output_dir / "tables").exists())
            self.assertFalse((output_dir / "artifacts").exists())

            with (
                fake_docling_runtime(),
                patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ),
            ):
                self.assertEqual(vlm_review.run_worker(args), 0)
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertTrue(status.get("ok"))
            self.assertTrue((output_dir / "document.json").exists())

    def test_source_mismatch_is_quarantined_and_next_retry_can_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"route-b-current-source")
            args = SimpleNamespace(
                worker_pdf=sample,
                worker_job_id="sample",
                output_root=root,
                artifacts_path=root,
                python=sys.executable,
                document_timeout=1500.0,
            )
            output_dir = root / "sample"
            output_dir.mkdir()
            (output_dir / "source.pdf").write_bytes(b"route-b-old-source")
            (output_dir / "document.json").write_text('{"old": true}', encoding="utf-8")
            (output_dir / "pages").mkdir()
            (output_dir / "pages" / "page_1.png").write_bytes(b"old-page")

            with patch.object(
                vlm_review,
                "model_selection",
                return_value=("granite_docling_mlx", [], True),
            ):
                self.assertEqual(vlm_review.run_worker(args), 1)
            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata.get("input_file_reference_mode"), "existing_mismatch")
            self.assertFalse(status.get("ok"))
            self.assertFalse((output_dir / "source.pdf").exists())
            quarantine_dirs = sorted(root.glob(".sample.vlm_quarantine_*"))
            self.assertTrue(quarantine_dirs)
            self.assertTrue((quarantine_dirs[-1] / "source.pdf").exists())
            self.assertTrue((quarantine_dirs[-1] / "pages" / "page_1.png").exists())

            with (
                fake_docling_runtime(),
                patch.object(
                    vlm_review,
                    "model_selection",
                    return_value=("granite_docling_mlx", [], True),
                ),
            ):
                self.assertEqual(vlm_review.run_worker(args), 0)
            self.assertEqual(_sha256(output_dir / "source.pdf"), _sha256(sample))
            self.assertTrue(json.loads((output_dir / "status.json").read_text(encoding="utf-8"))["ok"])

    def test_timeout_replaces_stale_output_and_orphan_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "document.pdf"
            sample.write_bytes(b"route-b-timeout-source")
            output_dir = root / "sample"
            output_dir.mkdir()
            os.link(str(sample), str(output_dir / "source.pdf"))
            (output_dir / "document.json").write_text('{"old": true}', encoding="utf-8")
            (output_dir / "pages").mkdir()
            (output_dir / "pages" / "page_8.png").write_bytes(b"old-page")
            orphan = root / ".sample.vlm_staging_dead_worker"
            orphan.mkdir()
            (orphan / "document.md").write_text("partial", encoding="utf-8")

            row = vlm_review.summarize_timeout(sample, "sample", output_dir, 12.5)

            self.assertFalse(row["ok"])
            self.assertEqual(row["success_class"], "timeout")
            self.assertTrue((output_dir / "source.pdf").exists())
            self.assertFalse((output_dir / "document.json").exists())
            self.assertFalse((output_dir / "pages").exists())
            self.assertFalse(orphan.exists())
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status.get("success_class"), "timeout")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
