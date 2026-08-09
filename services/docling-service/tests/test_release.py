from __future__ import annotations

import errno
from dataclasses import replace
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from docling_service.release import (
    JobManager,
    JobRecord,
    ReleaseConfig,
    build_adapter_command,
)


def make_config(root: Path, *, profile: str = "docker") -> ReleaseConfig:
    root.mkdir(parents=True, exist_ok=True)
    adapter = root / "quality_parity_adapter.py"
    adapter.write_text("# test adapter\n", encoding="utf-8")
    return ReleaseConfig(
        profile=profile,
        serve_url="http://backend:5001",
        adapter_path=adapter,
        input_root=root / "inputs",
        output_root=root / "outputs",
        state_root=root / "state",
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        conversion_timeout_seconds=60,
        image_export_mode="referenced",
        formula_policy=(
            "formula_service" if profile == "docker" else "granite_mlx"
        ),
        cn_ocr_parity=profile == "macos",
        api_token=None,
        formula_ocr_url="http://formula:8001" if profile == "docker" else None,
    )


class ReleaseConfigTests(unittest.TestCase):
    def test_default_image_transport_crosses_process_boundaries(self) -> None:
        for profile in ("docker", "macos"):
            with self.subTest(profile=profile):
                with patch.dict(
                    "os.environ",
                    {"DOCLING_RELEASE_PROFILE": profile},
                    clear=True,
                ):
                    self.assertEqual(
                        "embedded",
                        ReleaseConfig.from_env().image_export_mode,
                    )

    def test_profile_specific_timeout_defaults(self) -> None:
        with patch.dict("os.environ", {"DOCLING_RELEASE_PROFILE": "docker"}, clear=True):
            self.assertEqual(7200, ReleaseConfig.from_env().conversion_timeout_seconds)
        with patch.dict("os.environ", {"DOCLING_RELEASE_PROFILE": "macos"}, clear=True):
            self.assertEqual(3600, ReleaseConfig.from_env().conversion_timeout_seconds)

    def test_docker_rejects_macos_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            invalid = ReleaseConfig(
                **{
                    **config.__dict__,
                    "formula_policy": "granite_mlx",
                }
            )
            with self.assertRaisesRegex(ValueError, "MLX"):
                invalid.validate()

    def test_formula_sidecar_rejects_external_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            invalid = ReleaseConfig(
                **{
                    **config.__dict__,
                    "formula_ocr_url": "https://example.com/formulas",
                }
            )
            with self.assertRaisesRegex(ValueError, "local Docker/loopback"):
                invalid.validate()

    def test_platform_commands_keep_output_contract_but_change_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = JobRecord(
                job_id="d242f924-b5c6-48ba-9d45-b41034de8338",
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )
            docker_command = build_adapter_command(make_config(root / "docker"), record)
            mac_command = build_adapter_command(
                make_config(root / "mac", profile="macos"), record
            )
            self.assertIn("formula_service", docker_command)
            self.assertIn("http://formula:8001", docker_command)
            self.assertNotIn("--cn-ocr-parity", docker_command)
            self.assertIn("granite_mlx", mac_command)
            self.assertIn("--cn-ocr-parity", mac_command)
            self.assertIn("apply-all", docker_command)
            self.assertIn("apply-all", mac_command)

    def test_from_env_reads_upload_and_webhook_limits(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "DOCLING_RELEASE_PROFILE": "macos",
                "DOCLING_MAX_CONCURRENT_UPLOADS": "7",
                "DOCLING_MAX_WEBHOOK_SUBSCRIPTIONS": "9",
            },
            clear=True,
        ):
            config = ReleaseConfig.from_env()
            self.assertEqual(7, config.max_concurrent_uploads)
            self.assertEqual(9, config.max_webhook_subscriptions)


class JobManagerTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_staging_job_symlink_is_rejected_and_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            outside = root / "outside"
            outside.mkdir()
            for name, content in {
                "document.html": "<p>outside</p>",
                "document.md": "outside",
                "document.json": "{}",
                "metadata.json": "{}",
                "status.json": '{"ok": true}',
            }.items():
                (outside / name).write_text(content, encoding="utf-8")

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_root = Path(command[command.index("--output-root") + 1])
                (output_root / job_id).symlink_to(outside, target_is_directory=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            manager = JobManager(config, runner=runner)
            try:
                upload = root / "upload.pdf"
                upload.write_bytes(b"%PDF-1.7\n%%EOF\n")
                record = manager.create_job(upload, "symlink.pdf")
                for _attempt in range(100):
                    current = manager.get_job(record.job_id)
                    if current and current.state == "failed":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("symlink job did not fail")
                self.assertIn("symlink", (current.error or "").casefold())
                self.assertFalse((config.staging_root / record.job_id).exists())
                self.assertFalse((config.output_root / record.job_id).exists())
                self.assertTrue((outside / "document.md").exists())
            finally:
                manager.shutdown()

    def test_cross_filesystem_upload_copy_is_atomic_and_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(command, 1, "", "expected test failure")

            manager = JobManager(config, runner=runner)
            try:
                upload = root / "cross-volume-upload.pdf"
                content = b"%PDF-1.7\nCROSS-VOLUME\n%%EOF\n"
                upload.write_bytes(content)
                original_replace = Path.replace

                def cross_volume_once(path: Path, target: Path) -> Path:
                    if path == upload:
                        raise OSError(errno.EXDEV, "cross-device link")
                    return original_replace(path, target)

                with patch.object(Path, "replace", new=cross_volume_once):
                    record = manager.create_job(upload, "cross-volume.pdf")
                final_input = config.input_root / record.job_id / "source.pdf"
                self.assertEqual(content, final_input.read_bytes())
                self.assertFalse(upload.exists())
                self.assertEqual([], list(final_input.parent.glob(".*.part")))
            finally:
                manager.shutdown()

    def test_unexpected_runner_error_converges_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)

            def runner(_command, **_kwargs):
                raise RuntimeError("synthetic adapter crash")

            manager = JobManager(config, runner=runner)
            upload = root / "crash.pdf"
            upload.write_bytes(b"%PDF-1.7\n%%EOF\n")
            record = manager.create_job(upload, "crash.pdf")
            for _attempt in range(100):
                current = manager.get_job(record.job_id)
                if current and current.state == "failed":
                    break
                time.sleep(0.01)
            else:
                self.fail("unexpected runner failure did not become terminal")
            self.assertIn("RuntimeError", current.error or "")
            manager.shutdown()

    def test_job_state_is_persisted_and_outputs_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_root = Path(command[command.index("--output-root") + 1])
                output_dir = output_root / job_id
                output_dir.mkdir(parents=True)
                (output_dir / "document.html").write_text("<p>ok</p>", encoding="utf-8")
                (output_dir / "document.md").write_text("ok", encoding="utf-8")
                (output_dir / "document.json").write_text("{}", encoding="utf-8")
                (output_dir / "metadata.json").write_text("{}", encoding="utf-8")
                (output_dir / "status.json").write_text(
                    json.dumps({"ok": True}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "{}", "")

            manager = JobManager(config, runner=runner)
            upload = root / "upload.pdf"
            upload.write_bytes(b"%PDF-1.7\n%%EOF\n")
            record = manager.create_job(upload, "paper.pdf")
            for _attempt in range(100):
                current = manager.get_job(record.job_id)
                if current and current.state == "succeeded":
                    break
                time.sleep(0.01)
            else:
                self.fail("job did not finish")
            state_payload = json.loads(
                (config.state_root / "jobs" / f"{record.job_id}.json").read_text()
            )
            self.assertEqual("succeeded", state_payload["state"])
            outputs = manager.output_files(record.job_id)
            self.assertEqual("document.html", outputs[0]["path"])
            self.assertEqual(64, len(outputs[0]["sha256"]))
            with self.assertRaises(PermissionError):
                manager.resolve_output_file(record.job_id, "../../outside")
            if os.name != "nt":
                published = config.output_root / record.job_id
                outside_published = root / "outside-published"
                published.replace(outside_published)
                published.symlink_to(outside_published, target_is_directory=True)
                with self.assertRaises(PermissionError):
                    manager.resolve_output_file(record.job_id, "document.html")
            manager.shutdown()

    def test_staging_exceeding_output_limit_marks_failed_and_cleans_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "max_output_adapter.py"
            adapter.write_text(
                """
import sys
import time
from pathlib import Path

output_root = None
job_id = None
for index in range(len(sys.argv) - 1):
    if sys.argv[index] == "--output-root":
        output_root = Path(sys.argv[index + 1])
    elif sys.argv[index] == "--job-id":
        job_id = sys.argv[index + 1]

        if output_root is None or job_id is None:
            raise SystemExit(1)

job_dir = output_root / job_id
job_dir.mkdir(parents=True, exist_ok=True)
payload = job_dir / "document.md"
while True:
    with payload.open("ab") as handle:
        handle.write(b"x" * 1024)
        handle.flush()
        time.sleep(0.01)
""",
                encoding="utf-8",
            )
            config = replace(
                make_config(root),
                max_output_bytes=1024,
                adapter_path=adapter,
            )
            manager = JobManager(config)
            try:
                upload = root / "upload.pdf"
                upload.write_bytes(b"%PDF-1.7\n%%EOF\n")
                record = manager.create_job(upload, "too-large-output.pdf")
                for _ in range(200):
                    current = manager.get_job(record.job_id)
                    if current and current.state in {"failed", "succeeded"}:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("job did not finish")
                self.assertEqual("failed", current.state)
                self.assertIsNotNone(current.error)
                self.assertIn(
                    "DOCLING_MAX_OUTPUT_BYTES",
                    current.error or "",
                )
                self.assertFalse((config.staging_root / record.job_id).exists())
                self.assertFalse((config.output_root / record.job_id).exists())
            finally:
                manager.shutdown()

    def test_restart_marks_nonterminal_jobs_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            config.ensure_directories()
            state_path = config.state_root / "jobs" / "81e32a59-0e6e-4bff-9155-f98609dbf597.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "job_id": state_path.stem,
                        "state": "running",
                        "original_name": "paper.pdf",
                        "input_path": str(config.input_root / state_path.stem / "source.pdf"),
                        "output_dir": str(config.output_root / state_path.stem),
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
            manager = JobManager(config)
            record = manager.get_job(state_path.stem)
            self.assertIsNotNone(record)
            self.assertEqual("interrupted", record.state)
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
