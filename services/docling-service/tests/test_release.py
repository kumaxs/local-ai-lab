from __future__ import annotations

import json
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


class JobManagerTests(unittest.TestCase):
    def test_job_state_is_persisted_and_outputs_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_dir = config.output_root / job_id
                output_dir.mkdir(parents=True)
                (output_dir / "document.html").write_text("<p>ok</p>", encoding="utf-8")
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
            manager.shutdown()

    def test_restart_marks_nonterminal_jobs_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            config.ensure_directories()
            state_path = config.state_root / "jobs" / "81e32a59-0e6e-4bff-9155-f98609dbf597.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "job_id": state_path.stem,
                        "state": "running",
                        "original_name": "paper.pdf",
                        "input_path": "/tmp/input.pdf",
                        "output_dir": "/tmp/output",
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
