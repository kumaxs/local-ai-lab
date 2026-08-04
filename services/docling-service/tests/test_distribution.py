from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
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
    def test_release_versions_are_aligned(self) -> None:
        project_text = (SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project_version = re.search(r'^version = "([^"]+)"$', project_text, re.MULTILINE)
        self.assertIsNotNone(project_version)
        self.assertEqual("1.0.1", project_version.group(1))
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


if __name__ == "__main__":
    unittest.main()
