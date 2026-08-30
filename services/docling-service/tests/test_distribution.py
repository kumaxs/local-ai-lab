from __future__ import annotations

import hashlib
import json
import zipfile
import subprocess
import sys
import tarfile
import os
import shutil
import tempfile
import unittest
from pathlib import Path
import re

from docling_service.formula_api import FORMULA_SERVICE_VERSION
from docling_service.release import RELEASE_VERSION


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services/docling-service"
RELEASE_ROOT = SERVICE_ROOT / "release"


class DistributionTests(unittest.TestCase):
    INVENTORY_TOOL = (
        "docs/integrations/docling-serve-quality-parity/"
        "pdf_structure_inventory.py"
    )
    REGION_GATE_TOOL = (
        "docs/integrations/docling-serve-quality-parity/"
        "region_quality_gate.py"
    )
    UI_ASSETS = (
        "ui/index.html",
        "ui/main.js",
        "ui/styles.css",
    )

    def test_dockerignore_whitelists_inventory_script(self) -> None:
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn(f"!{self.INVENTORY_TOOL}", dockerignore)
        self.assertIn(f"!{self.REGION_GATE_TOOL}", dockerignore)

    def test_python_package_contains_webui_static_assets(self) -> None:
        """Build a wheel in a scratch tree and verify the served UI is packaged."""
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "service"
            wheel_dir = Path(directory) / "wheel"
            shutil.copytree(
                SERVICE_ROOT,
                scratch,
                ignore=shutil.ignore_patterns(
                    ".venv", "__pycache__", "*.pyc", "*.egg-info"
                ),
            )
            build_script = (
                "from pathlib import Path; import sys; "
                "from setuptools.build_meta import build_wheel; "
                "out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True); "
                "build_wheel(str(out))"
            )
            subprocess.run(
                [sys.executable, "-c", build_script, str(wheel_dir)],
                cwd=scratch,
                check=True,
                capture_output=True,
                text=True,
            )
            wheels = sorted(wheel_dir.glob("*.whl"))
            self.assertEqual(1, len(wheels))
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            for asset in self.UI_ASSETS:
                self.assertIn(f"docling_service/{asset}", names)

    def _get_service_block(self, compose_text: str, service: str) -> str:
        lines = compose_text.splitlines()
        start = None
        next_service = None
        for i, line in enumerate(lines):
            if line.startswith(f"  {service}:"):
                start = i
                break
        if start is None:
            raise AssertionError(f"Service {service} not found")
        for i in range(start + 1, len(lines)):
            if re.match(r"^  [a-zA-Z][^:]*:$", lines[i]):
                next_service = i
                break
        end = next_service if next_service is not None else len(lines)
        return "\n".join(lines[start:end]) + "\n"

    def test_release_versions_are_aligned(self) -> None:
        project_text = (SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project_version = re.search(r'^version = "([^"]+)"$', project_text, re.MULTILINE)
        self.assertIsNotNone(project_version)
        self.assertEqual("1.1.1", project_version.group(1))
        self.assertEqual(project_version.group(1), RELEASE_VERSION)
        self.assertEqual(RELEASE_VERSION, FORMULA_SERVICE_VERSION)
        for path in (
            SERVICE_ROOT / "deploy/docker/compose.release.yaml",
            RELEASE_ROOT / "BUNDLE_README.md",
            RELEASE_ROOT / "RELEASE_NOTES.md",
        ):
            self.assertIn(RELEASE_VERSION, path.read_text(encoding="utf-8"))
        installer = (RELEASE_ROOT / "install-macos.sh").read_text(encoding="utf-8")
        self.assertIn("VERSION=$(<${SCRIPT_DIR}/VERSION)", installer)

    def test_release_compose_uses_prebuilt_portable_images(self) -> None:
        compose = (SERVICE_ROOT / "deploy/docker/compose.release.yaml").read_text(encoding="utf-8")
        self.assertNotIn("build:", compose)
        self.assertEqual(3, compose.count("ghcr.io/kumaxs"))
        self.assertEqual(3, compose.count("local-ai-lab-docling-"))
        self.assertIn("docling-api", compose)
        self.assertIn("docling-backend", compose)
        self.assertIn("docling-formula", compose)
        for forbidden in ("ocrmac", "granite_mlx", "Metal", "Apple Vision"):
            self.assertNotIn(forbidden, compose)
        for required in (
            "DOCLING_INPUT_TTL_SECONDS",
            "DOCLING_SUCCESS_OUTPUT_TTL_SECONDS",
            "DOCLING_FAILED_OUTPUT_TTL_SECONDS",
            "DOCLING_JOB_TTL_SECONDS",
            "DOCLING_MAX_CONCURRENT_UPLOADS",
            "DOCLING_MAX_PENDING_JOBS",
            "DOCLING_MAX_OUTPUT_BYTES",
            "DOCLING_MAX_DATA_BYTES",
            "DOCLING_MIN_FREE_BYTES",
            "DOCLING_IDEMPOTENCY_TTL_SECONDS",
            "DOCLING_DOWNLOAD_LEASE_SECONDS",
            "DOCLING_WEBHOOK_ALLOWED_HOSTS",
            "DOCLING_WEBHOOK_MAX_ATTEMPTS",
            "DOCLING_MAX_WEBHOOK_SUBSCRIPTIONS",
        ):
            self.assertIn(required, compose)

    def test_docker_python_base_is_pinned_to_debian_suite(self) -> None:
        for name in ("Dockerfile.api", "Dockerfile.backend", "Dockerfile.formula"):
            with self.subTest(dockerfile=name):
                first = (SERVICE_ROOT / "deploy/docker" / name).read_text(
                    encoding="utf-8"
                ).splitlines()[0]
                self.assertEqual("FROM python:3.12-slim-bookworm", first)

    def test_docker_compose_defaults_hugging_face_to_mirror_with_override(self) -> None:
        expected = 'HF_ENDPOINT: "${HF_ENDPOINT:-https://hf-mirror.com}"'
        for relative in ("deploy/docker/compose.yaml", "deploy/docker/compose.release.yaml"):
            with self.subTest(compose=relative):
                compose = (SERVICE_ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(2, compose.count(expected))

    def test_docker_compose_logging_limits_are_configured(self) -> None:
        for relative in ("deploy/docker/compose.yaml", "deploy/docker/compose.release.yaml"):
            with self.subTest(compose=relative):
                compose = (SERVICE_ROOT / relative).read_text(encoding="utf-8")
                for service in ("backend", "formula", "api"):
                    with self.subTest(service=service):
                        block = self._get_service_block(compose, service)
                        self.assertIn("    logging:", block)
                        self.assertIn("      driver: json-file", block)
                        self.assertIn(
                            '        max-size: "${DOCLING_DOCKER_LOG_MAX_SIZE:-10m}"',
                            block,
                        )
                        self.assertIn(
                            '        max-file: "${DOCLING_DOCKER_LOG_MAX_FILE:-3}"',
                            block,
                        )

    def test_macos_start_uses_logging_wrapper(self) -> None:
        start_script = (SERVICE_ROOT / "deploy/macos/start.sh").read_text(encoding="utf-8")
        self.assertIn("logging_wrapper.py", start_script)
        self.assertIn("--log-path", start_script)

    def test_upload_spooling_uses_managed_state_temp(self) -> None:
        dockerfile = (SERVICE_ROOT / "deploy/docker/Dockerfile.api").read_text(
            encoding="utf-8"
        )
        self.assertIn("TMPDIR=/data/state/temp", dockerfile)
        self.assertIn("mkdir -p /data/inputs /data/outputs /data/state/temp", dockerfile)
        macos_script = (SERVICE_ROOT / "deploy/macos/run-api.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("mkdir -p ${DOCLING_STATE_ROOT}/temp", macos_script)
        self.assertIn("export TMPDIR=${DOCLING_STATE_ROOT}/temp", macos_script)

    def test_macos_logging_wrapper_bounds_output(self) -> None:
        wrapper = str(SERVICE_ROOT / "deploy/macos/logging_wrapper.py")
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            output = runtime / "docling-macos-logging-wrapper-test.log"
            # Simulate upgrading from the previous unbounded log behavior.
            output.write_bytes(b"legacy-log-data\n" * 1000)
            producer = runtime / "docling-macos-logging-wrapper-source.py"
            producer.write_text(
                "\n".join(
                    [
                        "import sys",
                        "for i in range(80):",
                        "    sys.stdout.write('stdout-%03d-' % i + 'A' * 300)",
                        "    sys.stdout.write('\\n')",
                        "    sys.stderr.write('stderr-%03d-' % i + 'B' * 300)",
                        "    sys.stderr.write('\\n')",
                        "    sys.stdout.flush()",
                        "    sys.stderr.flush()",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["DOCLING_MACOS_LOG_MAX_BYTES"] = "4096"
            env["DOCLING_MACOS_LOG_BACKUP_COUNT"] = "2"
            command = [
                sys.executable,
                wrapper,
                "--log-path",
                str(output),
                "--",
                sys.executable,
                str(producer),
            ]
            subprocess.run(command, check=True, env=env, capture_output=True, text=True)

            logs = sorted(runtime.glob("docling-macos-logging-wrapper-test.log*"))
            self.assertGreaterEqual(len(logs), 2)
            self.assertLessEqual(len(logs), 3)
            for path in logs:
                self.assertLessEqual(path.stat().st_size, 4096)
            content = "".join(path.read_text(encoding="utf-8") for path in logs)
            self.assertIn("stdout-079", content)
            self.assertIn("stderr-079", content)

    def test_macos_logging_wrapper_script_is_syntax_valid(self) -> None:
        wrapper = str(SERVICE_ROOT / "deploy/macos/logging_wrapper.py")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; compile(Path(__import__('sys').argv[1]).read_text(encoding='utf-8'), __import__('sys').argv[1], 'exec')",
                wrapper,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_docker_api_image_includes_inventory_script(self) -> None:
        dockerfile = (SERVICE_ROOT / "deploy/docker/Dockerfile.api").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "COPY docs/integrations/docling-serve-quality-parity/"
            "pdf_structure_inventory.py /opt/docling-quality/pdf_structure_inventory.py",
            dockerfile,
        )
        self.assertIn(
            "COPY docs/integrations/docling-serve-quality-parity/"
            "region_quality_gate.py /opt/docling-quality/region_quality_gate.py",
            dockerfile,
        )

    def test_release_workflow_publishes_assets_and_multiarch_images(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/docling-service-release.yml"
        if not workflow_path.is_file():
            self.skipTest("release workflow is intentionally not part of the install bundle")
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn('"v*.*.*"', workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("linux/amd64,linux/arm64", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("provenance: mode=max", workflow)
        self.assertIn("sbom: true", workflow)

    def test_bundle_builds_and_verifies_without_runtime_or_private_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "first"
            second_output = Path(directory) / "second"
            command = [
                sys.executable,
                str(RELEASE_ROOT / "build_release_bundle.py"),
                "--source-root",
                str(REPO_ROOT),
                "--output-dir",
                str(output),
                "--version",
                RELEASE_VERSION,
                "--commit",
                "0" * 40,
                "--epoch",
                "1785816000",
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            second_command = list(command)
            second_command[second_command.index(str(output))] = str(second_output)
            subprocess.run(second_command, check=True, capture_output=True, text=True)
            tar_path = output / f"docling-service-{RELEASE_VERSION}.tar.gz"
            zip_path = output / f"docling-service-{RELEASE_VERSION}.zip"
            subprocess.run(
                [
                    sys.executable,
                    str(RELEASE_ROOT / "verify_release_bundle.py"),
                    "--checksums",
                    str(output / "SHA256SUMS"),
                    str(tar_path),
                    str(zip_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            checksums = (output / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertIn(hashlib.sha256(tar_path.read_bytes()).hexdigest(), checksums)
            for name in (
                tar_path.name,
                zip_path.name,
                "SHA256SUMS",
                f"{tar_path.name}.sha256",
                f"{zip_path.name}.sha256",
            ):
                self.assertEqual((output / name).read_bytes(), (second_output / name).read_bytes())
            with tarfile.open(tar_path, "r:gz") as archive:
                names = archive.getnames()
                manifest_name = next(name for name in names if name.endswith("/RELEASE_MANIFEST.json"))
                manifest = json.load(archive.extractfile(manifest_name))
            self.assertEqual(RELEASE_VERSION, manifest["version"])
            self.assertEqual(["linux/amd64", "linux/arm64"], manifest["docker_platforms"])
            self.assertFalse(any("/.runtime/" in name or "/reports/" in name for name in names))
            self.assertFalse(any(name.endswith((".pdf", ".log", ".pyc")) for name in names))
            self.assertIn(
                f"docling-service-{RELEASE_VERSION}/services/docling-service/deploy/macos/logging_wrapper.py",
                names,
            )
            inventory_bundle_path = (
                f"docling-service-{RELEASE_VERSION}/{self.INVENTORY_TOOL}"
            )
            self.assertIn(inventory_bundle_path, names)
            region_gate_bundle_path = (
                f"docling-service-{RELEASE_VERSION}/{self.REGION_GATE_TOOL}"
            )
            self.assertIn(region_gate_bundle_path, names)
            ui_bundle_paths = {
                f"docling-service-{RELEASE_VERSION}/services/docling-service/docling_service/{asset}"
                for asset in self.UI_ASSETS
            }
            self.assertTrue(ui_bundle_paths.issubset(names))
            manifest_paths = {entry.get("path") for entry in manifest["files"]}
            self.assertIn(self.INVENTORY_TOOL, manifest_paths)
            self.assertIn(self.REGION_GATE_TOOL, manifest_paths)
            self.assertTrue(
                {
                    f"services/docling-service/docling_service/{asset}"
                    for asset in self.UI_ASSETS
                }.issubset(manifest_paths)
            )
            with zipfile.ZipFile(zip_path) as archive:
                zip_names = set(archive.namelist())
            self.assertTrue(ui_bundle_paths.issubset(zip_names))

    def test_release_verification_rejects_missing_inventory_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            command = [
                sys.executable,
                str(RELEASE_ROOT / "build_release_bundle.py"),
                "--source-root",
                str(REPO_ROOT),
                "--output-dir",
                str(output),
                "--version",
                RELEASE_VERSION,
                "--commit",
                "0" * 40,
                "--epoch",
                "1785816000",
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            source_zip = output / f"docling-service-{RELEASE_VERSION}.zip"
            bad_zip = output / f"docling-service-{RELEASE_VERSION}-missing-inventory.zip"
            with zipfile.ZipFile(source_zip, "r") as source:
                with zipfile.ZipFile(
                    bad_zip,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as target:
                    for item in source.infolist():
                        if item.filename.endswith(f"/{self.INVENTORY_TOOL}"):
                            continue
                        target.writestr(item, source.read(item.filename))
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(
                    [
                        sys.executable,
                        str(RELEASE_ROOT / "verify_release_bundle.py"),
                        str(bad_zip),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_release_verification_rejects_missing_webui_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            subprocess.run(
                [
                    sys.executable,
                    str(RELEASE_ROOT / "build_release_bundle.py"),
                    "--source-root",
                    str(REPO_ROOT),
                    "--output-dir",
                    str(output),
                    "--version",
                    RELEASE_VERSION,
                    "--commit",
                    "0" * 40,
                    "--epoch",
                    "1785816000",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            source_zip = output / f"docling-service-{RELEASE_VERSION}.zip"
            bad_zip = output / f"docling-service-{RELEASE_VERSION}-missing-ui.zip"
            missing_suffix = "/services/docling-service/docling_service/ui/index.html"
            with zipfile.ZipFile(source_zip, "r") as source:
                with zipfile.ZipFile(
                    bad_zip,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as target:
                    for item in source.infolist():
                        if item.filename.endswith(missing_suffix):
                            continue
                        target.writestr(item, source.read(item.filename))
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(
                    [
                        sys.executable,
                        str(RELEASE_ROOT / "verify_release_bundle.py"),
                        str(bad_zip),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )


if __name__ == "__main__":
    unittest.main()
