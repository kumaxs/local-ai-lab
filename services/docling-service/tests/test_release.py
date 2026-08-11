from __future__ import annotations

import errno
import hashlib
from dataclasses import replace
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from docling_service.contract import REQUIRED_SUCCESS_OUTPUTS
from docling_service.release import (
    JobManager,
    JobRecord,
    ReleaseConfig,
    build_adapter_command,
)


def make_config(
    root: Path,
    *,
    profile: str = "docker",
    formula_second_pass_policy: str = "off",
    formula_second_pass_route_b_dir: Path | None = None,
) -> ReleaseConfig:
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
        formula_second_pass_policy=formula_second_pass_policy,
        formula_second_pass_route_b_dir=formula_second_pass_route_b_dir,
        cn_ocr_parity=profile == "macos",
        api_token=None,
        formula_ocr_url="http://formula:8001" if profile == "docker" else None,
    )


def write_trusted_route_b_artifact(
    directory: Path,
    *,
    ok: bool = True,
    job_id: str | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "document.json").write_text("{}", encoding="utf-8")
    (directory / "status.json").write_text(
        json.dumps({"ok": ok}),
        encoding="utf-8",
    )
    if job_id is not None:
        (directory / "metadata.json").write_text(
            json.dumps({"job_id": job_id}),
            encoding="utf-8",
        )


def write_success_outputs(
    directory: Path,
    *,
    original_input_sha256: str | None = None,
    visual_evidence_input_sha256: str | None = None,
    conversion_input_sha256: str | None = None,
    source_pdf_bytes: bytes | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    source_pdf_bytes = source_pdf_bytes or b"%PDF-1.7\n%%EOF\n"
    payload = {
        "document.html": "<p>ok</p>",
        "document.md": "ok",
        "document.json": "{}",
    }
    for name in REQUIRED_SUCCESS_OUTPUTS:
        if name in payload:
            (directory / name).write_text(payload[name], encoding="utf-8")
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "original_input_sha256": original_input_sha256,
                "visual_evidence_input_sha256": (
                    visual_evidence_input_sha256
                    if visual_evidence_input_sha256 is not None
                    else original_input_sha256
                ),
                "conversion_input_sha256": conversion_input_sha256
                if conversion_input_sha256 is not None
                else original_input_sha256,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / "status.json").write_text(
        json.dumps({"ok": True}), encoding="utf-8"
    )
    (directory / "source.pdf").write_bytes(source_pdf_bytes)


class ReleaseConfigTests(unittest.TestCase):
    def test_legacy_formula_second_pass_policies_are_normalized(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "DOCLING_RELEASE_PROFILE": "docker",
                "DOCLING_FORMULA_SECOND_PASS_POLICY": "review",
            },
            clear=True,
        ):
            self.assertEqual("auto", ReleaseConfig.from_env().formula_second_pass_policy)
        with patch.dict(
            "os.environ",
            {
                "DOCLING_RELEASE_PROFILE": "docker",
                "DOCLING_FORMULA_SECOND_PASS_POLICY": "apply",
                "DOCLING_FORMULA_OCR_URL": "http://formula:8001",
            },
            clear=True,
        ):
            self.assertEqual("apply-all", ReleaseConfig.from_env().formula_second_pass_policy)

    def test_release_config_keeps_positional_order_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "quality_parity_adapter.py"
            adapter.write_text("# test adapter", encoding="utf-8")
            config = ReleaseConfig(
                "docker",
                "http://backend:5001",
                adapter,
                root / "inputs",
                root / "outputs",
                root / "state",
                1024,
                1,
                60,
                "referenced",
                "formula_service",
                False,
                "test-token",
                "http://formula:8001",
                2,
            )
            self.assertEqual("off", config.formula_second_pass_policy)
            self.assertIsNone(config.formula_second_pass_route_b_dir)
            self.assertEqual(2, config.max_concurrent_uploads)

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

    def test_formula_second_pass_policy_defaults_to_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"DOCLING_RELEASE_PROFILE": "docker"}, clear=True):
                self.assertEqual("off", ReleaseConfig.from_env().formula_second_pass_policy)
            with patch.dict("os.environ", {"DOCLING_RELEASE_PROFILE": "macos"}, clear=True):
                self.assertEqual("off", ReleaseConfig.from_env().formula_second_pass_policy)
            root = Path(directory)
            config = make_config(root)
            config2 = make_config(root / "auto-missing", formula_second_pass_policy="auto")
            self.assertEqual("off", config.effective_formula_second_pass_policy())
            self.assertEqual("off", config2.effective_formula_second_pass_policy())

    def test_effective_formula_second_pass_policy_requires_global_route_b_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            route_b_dir.mkdir()
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            self.assertEqual("off", config.effective_formula_second_pass_policy())

    def test_no_job_policy_requires_regular_provenance_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            route_b_dir.mkdir()
            (route_b_dir / "document.json").write_text("{}", encoding="utf-8")
            (route_b_dir / "status.json").write_text(
                json.dumps({"ok": True}), encoding="utf-8"
            )
            config = make_config(
                root / "config",
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_dir,
            )

            self.assertEqual("off", config.effective_formula_second_pass_policy())

            (route_b_dir / "metadata.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                "apply-all", config.effective_formula_second_pass_policy()
            )

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_validate_rejects_configured_route_b_root_symlink_early(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "route-b-target"
            target.mkdir()
            configured = root / "route-b-link"
            configured.symlink_to(target, target_is_directory=True)
            config = make_config(
                root / "config",
                formula_second_pass_policy="apply-all",
                formula_second_pass_route_b_dir=configured,
            )

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                config.validate()

    def test_formula_second_pass_route_b_env_path_is_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b = root / "route-b"
            route_b.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(
                    "os.environ",
                    {
                        "DOCLING_RELEASE_PROFILE": "docker",
                        "DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR": "route-b",
                    },
                    clear=True,
                ):
                    config = ReleaseConfig.from_env()
            finally:
                os.chdir(original_cwd)

            self.assertTrue(config.formula_second_pass_route_b_dir.is_absolute())
            self.assertTrue(config.formula_second_pass_route_b_dir.samefile(route_b))

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_configured_route_b_root_symlink_is_rejected_for_all_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )
            for layout in ("direct", "shared"):
                with self.subTest(layout=layout):
                    target = root / f"route-b-{layout}-target"
                    if layout == "direct":
                        write_trusted_route_b_artifact(target, job_id=job_id)
                    else:
                        write_trusted_route_b_artifact(target / job_id, job_id=job_id)
                    configured = root / f"route-b-{layout}"
                    configured.symlink_to(target, target_is_directory=True)
                    config = make_config(
                        root / f"config-{layout}",
                        formula_second_pass_policy="auto",
                        formula_second_pass_route_b_dir=configured,
                    )

                    with self.assertRaisesRegex(
                        ValueError,
                        "Configured Route-B root must not be a symlink",
                    ):
                        config.effective_formula_second_pass_policy(job_id)
                    with self.assertRaisesRegex(
                        ValueError,
                        "Configured Route-B root must not be a symlink",
                    ):
                        build_adapter_command(config, record)

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_env_route_b_root_symlink_remains_visible_to_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "route-b-target"
            target.mkdir()
            configured = root / "route-b-link"
            configured.symlink_to(target, target_is_directory=True)
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(
                    "os.environ",
                    {
                        "DOCLING_RELEASE_PROFILE": "docker",
                        "DOCLING_FORMULA_SECOND_PASS_POLICY": "auto",
                        "DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR": configured.name,
                    },
                    clear=True,
                ):
                    config = ReleaseConfig.from_env()
            finally:
                os.chdir(original_cwd)

            self.assertTrue(config.formula_second_pass_route_b_dir.is_symlink())
            with self.assertRaisesRegex(
                ValueError,
                "Configured Route-B root must not be a symlink",
            ):
                config.effective_formula_second_pass_policy("job-id")

    def test_formula_second_pass_policy_apply_all_rejects_missing_route_b_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory), formula_second_pass_policy="apply-all")
            with self.assertRaisesRegex(
                ValueError,
                "DOCLING_FORMULA_SECOND_PASS_POLICY=apply-all requires",
            ):
                config.validate()

    def test_formula_second_pass_policy_apply_all_adds_route_b_dir_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            job_id = "d242f924-b5c6-48ba-9d45-b41034de8338"
            write_trusted_route_b_artifact(route_b_dir, job_id=job_id)
            config = make_config(
                root,
                formula_second_pass_policy="apply-all",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )
            command = build_adapter_command(config, record)
            policy_index = command.index("--formula-second-pass-policy") + 1
            self.assertEqual(
                "apply-all",
                config.effective_formula_second_pass_policy(record.job_id),
            )
            self.assertEqual("apply-all", command[policy_index])
            self.assertIn("--formula-second-pass-route-b-dir", command)
            route_b_index = command.index("--formula-second-pass-route-b-dir") + 1
            self.assertEqual(str(route_b_dir), command[route_b_index])

    def test_formula_second_pass_policy_auto_enables_when_route_b_dir_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            write_trusted_route_b_artifact(route_b_dir, job_id=job_id)
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )
            command = build_adapter_command(config, record)
            policy_index = command.index("--formula-second-pass-policy") + 1
            self.assertEqual("apply-all", command[policy_index])
            route_b_index = command.index("--formula-second-pass-route-b-dir") + 1
            self.assertEqual(str(route_b_dir), command[route_b_index])

    def test_formula_second_pass_auto_requires_trusted_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            route_b_dir.mkdir()
            (route_b_dir / "document.json").write_text("{}", encoding="utf-8")
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            command = build_adapter_command(config, record)
            self.assertEqual("off", config.effective_formula_second_pass_policy(job_id))
            self.assertEqual(
                "off",
                command[command.index("--formula-second-pass-policy") + 1],
            )

            (route_b_dir / "status.json").write_text(
                json.dumps({"ok": True}),
                encoding="utf-8",
            )
            command = build_adapter_command(config, record)
            self.assertEqual("off", config.effective_formula_second_pass_policy(job_id))
            self.assertEqual(
                "off",
                command[command.index("--formula-second-pass-policy") + 1],
            )
            explicit_config = replace(
                config,
                formula_second_pass_policy="apply-all",
            )
            self.assertEqual(
                "off",
                explicit_config.effective_formula_second_pass_policy(job_id),
            )
            with self.assertRaisesRegex(ValueError, "trusted status.json"):
                build_adapter_command(explicit_config, record)

            (route_b_dir / "status.json").write_text(
                json.dumps({"ok": False}),
                encoding="utf-8",
            )
            command = build_adapter_command(config, record)
            self.assertEqual("off", config.effective_formula_second_pass_policy(job_id))
            self.assertEqual(
                "off",
                command[command.index("--formula-second-pass-policy") + 1],
            )

    def test_formula_second_pass_apply_all_fails_for_untrusted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            write_trusted_route_b_artifact(
                route_b_dir,
                ok=False,
                job_id=job_id,
            )
            config = make_config(
                root,
                formula_second_pass_policy="apply-all",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            self.assertEqual("off", config.effective_formula_second_pass_policy(job_id))
            with self.assertRaisesRegex(ValueError, "trusted status.json"):
                build_adapter_command(config, record)

    def test_direct_route_b_metadata_job_id_must_match_current_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            write_trusted_route_b_artifact(route_b_dir, job_id="different-job")
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            mismatch_command = build_adapter_command(config, record)
            self.assertEqual("off", config.effective_formula_second_pass_policy(job_id))
            self.assertEqual(
                "off",
                mismatch_command[
                    mismatch_command.index("--formula-second-pass-policy") + 1
                ],
            )

            (route_b_dir / "metadata.json").write_text(
                json.dumps({"job_id": job_id}),
                encoding="utf-8",
            )
            match_command = build_adapter_command(config, record)
            self.assertEqual(
                "apply-all",
                config.effective_formula_second_pass_policy(job_id),
            )
            self.assertEqual(
                "apply-all",
                match_command[match_command.index("--formula-second-pass-policy") + 1],
            )

    def test_formula_second_pass_route_b_root_resolves_per_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            job_route_b = route_b_root / job_id
            write_trusted_route_b_artifact(job_route_b, job_id=job_id)
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_root,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            command = build_adapter_command(config, record)

            route_b_index = command.index("--formula-second-pass-route-b-dir") + 1
            self.assertEqual(str(job_route_b), command[route_b_index])
            self.assertEqual(
                "apply-all",
                command[command.index("--formula-second-pass-policy") + 1],
            )

    def test_per_job_route_b_requires_matching_metadata_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )
            for metadata_job_id in (None, "stale-job"):
                case_name = "missing" if metadata_job_id is None else "stale"
                with self.subTest(case_name=case_name):
                    route_b_root = root / case_name / "route_b"
                    job_route_b = route_b_root / job_id
                    write_trusted_route_b_artifact(
                        job_route_b,
                        job_id=metadata_job_id,
                    )
                    config = make_config(
                        root / case_name,
                        formula_second_pass_policy="auto",
                        formula_second_pass_route_b_dir=route_b_root,
                    )

                    command = build_adapter_command(config, record)
                    self.assertEqual(
                        "off",
                        config.effective_formula_second_pass_policy(job_id),
                    )
                    self.assertEqual(
                        "off",
                        command[
                            command.index("--formula-second-pass-policy") + 1
                        ],
                    )
                    self.assertNotIn(
                        "--formula-second-pass-route-b-dir",
                        command,
                    )

                    explicit_config = replace(
                        config,
                        formula_second_pass_policy="apply-all",
                    )
                    self.assertEqual(
                        "off",
                        explicit_config.effective_formula_second_pass_policy(job_id),
                    )
                    with self.assertRaisesRegex(ValueError, "trusted status.json"):
                        build_adapter_command(explicit_config, record)

    def test_effective_policy_uses_the_same_per_job_resolver_as_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            job_route_b = route_b_root / job_id
            write_trusted_route_b_artifact(job_route_b, job_id=job_id)
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_root,
            )

            self.assertEqual("off", config.effective_formula_second_pass_policy())
            self.assertEqual(
                "apply-all",
                config.effective_formula_second_pass_policy(job_id),
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )
            command = build_adapter_command(config, record)
            self.assertEqual(
                config.effective_formula_second_pass_policy(job_id),
                command[command.index("--formula-second-pass-policy") + 1],
            )

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_per_job_route_b_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            route_b_root.mkdir()
            external = root / "external"
            external.mkdir()
            (external / "document.json").write_text("{}", encoding="utf-8")
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            (route_b_root / job_id).symlink_to(external, target_is_directory=True)
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_root,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            with self.assertRaisesRegex(ValueError, "escapes the configured root"):
                config.effective_formula_second_pass_policy(job_id)
            with self.assertRaisesRegex(ValueError, "escapes the configured root"):
                build_adapter_command(config, record)

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_per_job_route_b_internal_symlink_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            target = route_b_root / "different-job"
            write_trusted_route_b_artifact(target, job_id="different-job")
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            (route_b_root / job_id).symlink_to(target, target_is_directory=True)
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_root,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                config.effective_formula_second_pass_policy(job_id)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                build_adapter_command(config, record)

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_per_job_route_b_document_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            job_route_b = route_b_root / job_id
            job_route_b.mkdir(parents=True)
            external = root / "external-document.json"
            external.write_text("{}", encoding="utf-8")
            (job_route_b / "document.json").symlink_to(external)
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_root,
            )

            with self.assertRaisesRegex(ValueError, "escapes the configured root"):
                config.effective_formula_second_pass_policy(job_id)

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_route_b_document_and_status_must_be_non_symlink_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            for symlink_name, target_payload, other_payload in (
                ("document.json", "{}", {"ok": True}),
                ("status.json", json.dumps({"ok": True}), {}),
            ):
                with self.subTest(symlink_name=symlink_name):
                    route_b_dir = root / symlink_name.removesuffix(".json")
                    route_b_dir.mkdir()
                    target = route_b_dir / f"{symlink_name}.target"
                    target.write_text(target_payload, encoding="utf-8")
                    (route_b_dir / symlink_name).symlink_to(target)
                    other_name = (
                        "status.json"
                        if symlink_name == "document.json"
                        else "document.json"
                    )
                    (route_b_dir / other_name).write_text(
                        json.dumps(other_payload),
                        encoding="utf-8",
                    )
                    config = make_config(
                        root / f"config-{symlink_name}",
                        formula_second_pass_policy="auto",
                        formula_second_pass_route_b_dir=route_b_dir,
                    )
                    record = JobRecord(
                        job_id=job_id,
                        state="queued",
                        original_name="paper.pdf",
                        input_path=str(root / "paper.pdf"),
                        output_dir=str(root / "outputs"),
                        created_at="2026-08-01T00:00:00+00:00",
                    )

                    command = build_adapter_command(config, record)
                    self.assertEqual(
                        "off",
                        config.effective_formula_second_pass_policy(job_id),
                    )
                    self.assertEqual(
                        "off",
                        command[
                            command.index("--formula-second-pass-policy") + 1
                        ],
                    )

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_route_b_status_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            route_b_dir.mkdir()
            (route_b_dir / "document.json").write_text("{}", encoding="utf-8")
            external_status = root / "external-status.json"
            external_status.write_text(
                json.dumps({"ok": True}),
                encoding="utf-8",
            )
            (route_b_dir / "status.json").symlink_to(external_status)
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_dir,
            )

            with self.assertRaisesRegex(ValueError, "escapes the configured root"):
                config.effective_formula_second_pass_policy(job_id)

    @unittest.skipIf(os.name == "nt", "symlink behavior requires POSIX")
    def test_off_policy_does_not_resolve_route_b_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            route_b_root.mkdir()
            job_id = "a3c9c2b8-6e4d-4ec5-b6cf-0b9dfb9f6fd2"
            external = root / "external"
            external.mkdir()
            (external / "document.json").write_text("{}", encoding="utf-8")
            (route_b_root / job_id).symlink_to(external, target_is_directory=True)
            config = make_config(
                root,
                formula_second_pass_policy="off",
                formula_second_pass_route_b_dir=route_b_root,
            )
            record = JobRecord(
                job_id=job_id,
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            command = build_adapter_command(config, record)
            self.assertEqual(
                "off",
                command[command.index("--formula-second-pass-policy") + 1],
            )
            self.assertNotIn("--formula-second-pass-route-b-dir", command)

    def test_formula_second_pass_auto_is_off_without_matching_job_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_root = root / "route_b"
            route_b_root.mkdir()
            config = make_config(
                root,
                formula_second_pass_policy="auto",
                formula_second_pass_route_b_dir=route_b_root,
            )
            record = JobRecord(
                job_id="missing-job",
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
            )

            command = build_adapter_command(config, record)

            self.assertEqual(
                "off",
                command[command.index("--formula-second-pass-policy") + 1],
            )
            self.assertNotIn("--formula-second-pass-route-b-dir", command)

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
            self.assertIn("--formula-second-pass-policy", docker_command)
            self.assertEqual(
                "off",
                docker_command[docker_command.index("--formula-second-pass-policy") + 1],
            )
            self.assertNotIn("--formula-second-pass-route-b-dir", docker_command)
            self.assertIn("--formula-second-pass-policy", mac_command)
            self.assertEqual(
                "off",
                mac_command[mac_command.index("--formula-second-pass-policy") + 1],
            )
            self.assertNotIn("--formula-second-pass-route-b-dir", mac_command)

    def test_command_includes_expected_input_sha256_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected_sha256 = "c" * 64
            record = JobRecord(
                job_id="d242f924-b5c6-48ba-9d45-b41034de8338",
                state="queued",
                original_name="paper.pdf",
                input_path=str(root / "paper.pdf"),
                output_dir=str(root / "outputs"),
                created_at="2026-08-01T00:00:00+00:00",
                input_sha256=expected_sha256,
            )
            command = build_adapter_command(make_config(root), record)
            self.assertIn("--expected-input-sha256", command)
            index = command.index("--expected-input-sha256")
            self.assertEqual(expected_sha256, command[index + 1])

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

    def test_missing_route_b_artifact_marks_apply_all_job_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_b_dir = root / "route_b"
            route_b_dir.mkdir()
            config = make_config(
                root,
                formula_second_pass_policy="apply-all",
                formula_second_pass_route_b_dir=route_b_dir,
            )
            self.assertEqual(
                "off",
                config.effective_formula_second_pass_policy("missing-job"),
            )
            runner_called = False

            def runner(command, **_kwargs):
                nonlocal runner_called
                runner_called = True
                return subprocess.CompletedProcess(command, 0, "", "")

            manager = JobManager(config, runner=runner)
            try:
                upload = root / "upload.pdf"
                upload.write_bytes(b"%PDF-1.7\n%%EOF\n")
                record = manager.create_job(upload, "paper.pdf")
                for _attempt in range(100):
                    current = manager.get_job(record.job_id)
                    if current and current.state == "failed":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("missing route-b artifact job did not fail")
                self.assertIn(
                    "matching Route-B document directory",
                    current.error or "",
                )
                self.assertFalse(runner_called)
                self.assertFalse((config.staging_root / record.job_id).exists())
                self.assertFalse((config.output_root / record.job_id).exists())
            finally:
                manager.shutdown()

    def test_validate_success_outputs_checks_expected_input_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                conversion_input_sha256="b" * 64,
                source_pdf_bytes=source_pdf_bytes,
            )

            manifest = manager._validate_success_outputs(
                output_dir,
                expected_input_sha256=expected_input_sha256,
            )
            self.assertTrue(any(entry["path"] == "document.html" for entry in manifest))
            manager.shutdown()

    def test_validate_success_outputs_rejects_original_input_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256="b" * 64,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            with self.assertRaisesRegex(
                ValueError,
                "metadata.original_input_sha256 does not match expected input",
            ):
                manager._validate_success_outputs(
                    output_dir,
                    expected_input_sha256=expected_input_sha256,
                )
            manager.shutdown()

    def test_validate_success_outputs_rejects_visual_input_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256="b" * 64,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            with self.assertRaisesRegex(
                ValueError,
                "metadata.visual_evidence_input_sha256 does not match expected input",
            ):
                manager._validate_success_outputs(
                    output_dir,
                    expected_input_sha256=expected_input_sha256,
                )
            manager.shutdown()

    def test_job_state_is_persisted_and_outputs_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_root = Path(command[command.index("--output-root") + 1])
                input_file = Path(command[command.index("--input-file") + 1])
                source_pdf_bytes = input_file.read_bytes()
                expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
                output_dir = output_root / job_id
                write_success_outputs(
                    output_dir,
                    original_input_sha256=expected_input_sha256,
                    visual_evidence_input_sha256=expected_input_sha256,
                    conversion_input_sha256=expected_input_sha256,
                    source_pdf_bytes=source_pdf_bytes,
                )
                return subprocess.CompletedProcess(command, 0, "", "")

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

    def test_run_validates_success_outputs_against_stored_input_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            upload_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(upload_bytes).hexdigest()
            mismatch_input_sha256 = (
                "1" * 64 if expected_input_sha256 != ("1" * 64) else "2" * 64
            )

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_root = Path(command[command.index("--output-root") + 1])
                output_dir = output_root / job_id
                write_success_outputs(
                    output_dir,
                    original_input_sha256=mismatch_input_sha256,
                    visual_evidence_input_sha256=mismatch_input_sha256,
                    conversion_input_sha256=mismatch_input_sha256,
                    source_pdf_bytes=upload_bytes,
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            manager = JobManager(config, runner=runner)
            try:
                upload = root / "upload.pdf"
                upload.write_bytes(upload_bytes)
                record = manager.create_job(upload, "paper.pdf")
                for _attempt in range(100):
                    current = manager.get_job(record.job_id)
                    if current and current.state == "failed":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("run job with mismatched metadata did not fail")
                self.assertIn(
                    "does not match expected input",
                    current.error or "",
                )
            finally:
                manager.shutdown()

    def test_recovery_uses_input_sha256_from_job_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            mismatched_input_sha256 = "b" * 64
            job_id = "a11a1111-1111-4111-9111-a11111111111"

            manager = JobManager(config)
            create_result = manager.store.create_job_with_idempotency(
                idempotency_key=f"recovery-{job_id}",
                job_id=job_id,
                original_name="paper.pdf",
                input_path=str(config.input_root / job_id / "source.pdf"),
                output_dir=str(config.output_root / job_id),
                input_sha256=expected_input_sha256,
                input_size_bytes=32,
                reserved_output_bytes=config.max_output_bytes,
                created_at="2026-08-01T00:00:00+00:00",
            )
            self.assertFalse(create_result.get("error"))
            manager.store.update_job(job_id, state="running")
            write_success_outputs(
                config.output_root / job_id,
                original_input_sha256=mismatched_input_sha256,
                visual_evidence_input_sha256=mismatched_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            manager.shutdown()

            recovering = JobManager(config)
            try:
                for _attempt in range(100):
                    current = recovering.get_job(job_id)
                    if current and current.state in {"interrupted", "succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("recovery did not finalize job")
                self.assertEqual("interrupted", current.state)
            finally:
                recovering.shutdown()

    def test_validate_success_outputs_rejects_missing_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            (output_dir / "source.pdf").unlink()
            with self.assertRaisesRegex(ValueError, "source.pdf is missing"):
                manager._validate_success_outputs(
                    output_dir,
                    expected_input_sha256=expected_input_sha256,
                )
            manager.shutdown()

    def test_validate_success_outputs_rejects_symlinked_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            symlink_target = output_dir / "source_target.pdf"
            symlink_target.write_bytes(source_pdf_bytes)
            (output_dir / "source.pdf").unlink()
            (output_dir / "source.pdf").symlink_to(symlink_target)
            with self.assertRaisesRegex(ValueError, "source.pdf is not a regular file"):
                manager._validate_success_outputs(
                    output_dir,
                    expected_input_sha256=expected_input_sha256,
                )
            manager.shutdown()

    @unittest.skipIf(os.name == "nt", "POSIX permissions required for unreadable fixture")
    def test_validate_success_outputs_rejects_unreadable_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            source_pdf_path = output_dir / "source.pdf"
            source_pdf_path.chmod(0)
            try:
                with self.assertRaisesRegex(
                    ValueError, "source.pdf is not readable"
                ):
                    manager._validate_success_outputs(
                        output_dir,
                        expected_input_sha256=expected_input_sha256,
                    )
            finally:
                source_pdf_path.chmod(0o600)
            manager.shutdown()

    def test_validate_success_outputs_rejects_non_regular_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            (output_dir / "source.pdf").unlink()
            (output_dir / "source.pdf").mkdir()
            with self.assertRaisesRegex(ValueError, "source.pdf is not a regular file"):
                manager._validate_success_outputs(
                    output_dir,
                    expected_input_sha256=expected_input_sha256,
                )
            manager.shutdown()

    def test_validate_success_outputs_rejects_source_pdf_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            source_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                root / "outputs",
                original_input_sha256=source_input_sha256,
                visual_evidence_input_sha256=source_input_sha256,
                conversion_input_sha256=source_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )
            (root / "outputs" / "source.pdf").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "source.pdf does not match expected input"):
                manager._validate_success_outputs(
                    root / "outputs",
                    expected_input_sha256=source_input_sha256,
                )
            manager.shutdown()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "platform does not support O_NOFOLLOW")
    def test_validate_success_outputs_uses_nofollow_and_verifies_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )

            open_flags = []
            real_open = os.open

            def patched_open(path: str, flags: int, mode: int = 0o777) -> int:
                open_flags.append(flags)
                return real_open(path, flags, mode)

            with patch("docling_service.release.os.open", side_effect=patched_open):
                manager._validate_success_outputs(
                    output_dir,
                    expected_input_sha256=expected_input_sha256,
                )
            self.assertTrue(open_flags)
            self.assertNotEqual(open_flags[0] & os.O_NOFOLLOW, 0)
            self.assertNotEqual(open_flags[0] & os.O_CLOEXEC, 0)
            manager.shutdown()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "platform does not support O_NOFOLLOW")
    def test_validate_success_outputs_does_not_leak_file_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )

            real_open = os.open
            real_fdopen = os.fdopen
            open_count = 0
            reader_close_count = {"count": 0}
            open_fds: set[int] = set()

            def tracking_open(
                path: str, flags: int, mode: int = 0o777
            ) -> int:
                nonlocal open_count
                fd = real_open(path, flags, mode)
                open_count += 1
                open_fds.add(fd)
                return fd

            class TrackingHandle:
                def __init__(self, delegate: Any) -> None:
                    self._delegate = delegate
                    self._fd = delegate.fileno()
                    self._closed = False
                    self._close_count = reader_close_count
                    self._open_fds = open_fds

                def close(self) -> None:
                    if not self._closed:
                        self._closed = True
                        self._close_count["count"] += 1
                        self._open_fds.discard(self._fd)
                        self._delegate.close()

                def read(self, size: int = -1) -> bytes:
                    return self._delegate.read(size)

                def fileno(self) -> int:
                    return self._delegate.fileno()

                def __iter__(self):
                    return self._delegate.__iter__()

                def __next__(self):
                    return next(self._delegate)

                def __enter__(self) -> "TrackingHandle":
                    self._delegate.__enter__()
                    return self

                def __exit__(self, *args: Any) -> None:
                    self.close()
                    self._delegate.__exit__(*args)

            def tracking_fdopen(fd: int, mode: str, *args: Any, **kwargs: Any):
                return TrackingHandle(real_fdopen(fd, mode, *args, **kwargs))

            for _ in range(64):
                with patch(
                    "docling_service.release.os.open", side_effect=tracking_open
                ), patch("docling_service.release.os.fdopen", side_effect=tracking_fdopen):
                    manager._validate_success_outputs(
                        output_dir,
                        expected_input_sha256=expected_input_sha256,
                    )
                    self.assertFalse(open_fds)
            self.assertEqual(open_count, reader_close_count["count"])
            manager.shutdown()

    def test_validate_success_outputs_rejects_source_pdf_when_file_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            manager = JobManager(config)
            output_dir = root / "outputs"
            source_pdf_bytes = b"%PDF-1.7\n%%EOF\n"
            expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
            write_success_outputs(
                output_dir,
                original_input_sha256=expected_input_sha256,
                visual_evidence_input_sha256=expected_input_sha256,
                conversion_input_sha256=expected_input_sha256,
                source_pdf_bytes=source_pdf_bytes,
            )

            source_pdf_path = output_dir / "source.pdf"
            replacement_path = output_dir / "source-replaced.pdf"
            replacement_path.write_bytes(b"replacement " * 64)

            real_fdopen = os.fdopen
            real_fstat = os.fstat

            class TriggeringFile:
                def __init__(self, delegate: Any) -> None:
                    self._delegate = delegate
                    self._triggered = False

                def read(self, size: int = -1) -> bytes:
                    chunk = self._delegate.read(size)
                    if not self._triggered:
                        self._triggered = True
                        os.replace(replacement_path, source_pdf_path)
                    return chunk

                def fileno(self) -> int:
                    return self._delegate.fileno()

                def close(self) -> None:
                    return self._delegate.close()

                def __enter__(self) -> "TriggeringFile":
                    self._delegate.__enter__()
                    return self

                def __exit__(self, *args: Any) -> None:
                    return self._delegate.__exit__(*args)

                def __iter__(self):
                    return self._delegate.__iter__()

                def __next__(self):
                    return next(self._delegate)

            def patched_fdopen(fd: int, mode: str, *args: Any, **kwargs: Any):
                return TriggeringFile(real_fdopen(fd, mode, *args, **kwargs))

            def patched_fstat(fd: int):
                return real_fstat(fd)

            with patch("docling_service.release.os.fdopen", side_effect=patched_fdopen), patch(
                "docling_service.release.os.fstat", side_effect=patched_fstat
            ):
                with self.assertRaisesRegex(
                    ValueError, "source.pdf changed during verification"
                ):
                    manager._validate_success_outputs(
                        output_dir,
                        expected_input_sha256=expected_input_sha256,
                    )
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
