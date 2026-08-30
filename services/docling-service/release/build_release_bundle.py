#!/usr/bin/env python3
"""Build deterministic, self-contained Docling Service release archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SOURCE_PATHS = (
    Path(".dockerignore"),
    Path("services/docling-service/README.md"),
    Path("services/docling-service/pyproject.toml"),
    Path("services/docling-service/requirements.txt"),
    Path("services/docling-service/deploy"),
    Path("services/docling-service/docling_service"),
    Path("services/docling-service/docs"),
    Path("services/docling-service/release"),
    Path("services/docling-service/tests"),
    Path("docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py"),
    Path("docs/integrations/docling-serve-quality-parity/semantic_reflow.py"),
    Path("docs/integrations/docling-serve-quality-parity/formula_only_second_pass.py"),
    Path("docs/integrations/docling-serve-quality-parity/pdf_structure_inventory.py"),
    Path("docs/integrations/docling-serve-quality-parity/region_quality_gate.py"),
)
ROOT_TEMPLATE_MAP = {
    Path("services/docling-service/release/BUNDLE_README.md"): Path("README.md"),
    Path("services/docling-service/release/install-macos.sh"): Path("install-macos.sh"),
    Path("services/docling-service/release/docker-up.sh"): Path("docker-up.sh"),
    Path("services/docling-service/release/docker-down.sh"): Path("docker-down.sh"),
}
SKIP_PARTS = {".git", ".runtime", ".venv", "__pycache__", "reports"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pdf", ".log"}
REQUIRED_BUNDLE_FILES = {
    Path("README.md"),
    Path("VERSION"),
    Path("install-macos.sh"),
    Path("docker-up.sh"),
    Path("docker-down.sh"),
    Path("services/docling-service/deploy/docker/compose.release.yaml"),
    Path("services/docling-service/deploy/macos/install.sh"),
    Path("services/docling-service/docling_service/release.py"),
    Path("services/docling-service/docling_service/ui/index.html"),
    Path("services/docling-service/docling_service/ui/main.js"),
    Path("services/docling-service/docling_service/ui/styles.css"),
    Path("docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py"),
    Path("docs/integrations/docling-serve-quality-parity/pdf_structure_inventory.py"),
    Path("docs/integrations/docling-serve-quality-parity/region_quality_gate.py"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_root(script_path: Path | None = None) -> Path:
    script = script_path or Path(__file__)
    return script.resolve().parents[3]


def git_commit(source_root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source_root, text=True
    ).strip()


def should_include(path: Path) -> bool:
    return not (
        any(part in SKIP_PARTS for part in path.parts)
        or path.suffix.casefold() in SKIP_SUFFIXES
        or path.name == ".DS_Store"
    )


def iter_source_files(source_root: Path) -> list[Path]:
    files: set[Path] = set()
    for relative in SOURCE_PATHS:
        source = source_root / relative
        if not source.exists():
            raise FileNotFoundError(f"release input is missing: {relative}")
        if source.is_file():
            files.add(relative)
            continue
        for child in source.rglob("*"):
            if child.is_file():
                child_relative = child.relative_to(source_root)
                if should_include(child_relative):
                    files.add(child_relative)
    return sorted(files, key=lambda item: item.as_posix())


def copy_payload(source_root: Path, bundle_root: Path) -> None:
    for relative in iter_source_files(source_root):
        destination = bundle_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    for source_relative, destination_relative in ROOT_TEMPLATE_MAP.items():
        destination = bundle_root / destination_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / source_relative, destination)


def manifest_entries(bundle_root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(bundle_root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == "RELEASE_MANIFEST.json":
            continue
        relative = path.relative_to(bundle_root)
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256(path),
                "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & stat.S_IXUSR),
            }
        )
    return entries


def write_manifest(
    bundle_root: Path,
    *,
    version: str,
    commit: str,
    epoch: int,
    docker_platforms: list[str],
) -> None:
    payload = {
        "schema_version": 1,
        "product": "docling-service",
        "version": version,
        "git_commit": commit,
        "created_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "docker_images": {
            "api": f"ghcr.io/kumaxs/local-ai-lab-docling-api:{version}",
            "backend": f"ghcr.io/kumaxs/local-ai-lab-docling-backend:{version}",
            "formula": f"ghcr.io/kumaxs/local-ai-lab-docling-formula:{version}",
        },
        "docker_platforms": docker_platforms,
        "macos_release_target": "Apple Silicon; macOS 26.4 or newer",
        "files": manifest_entries(bundle_root),
    }
    (bundle_root / "RELEASE_MANIFEST.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def normalized_mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644


def build_tar(bundle_root: Path, destination: Path, epoch: int) -> None:
    with destination.open("wb") as raw_handle:
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", filename="", mtime=epoch) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as archive:
                paths = [bundle_root, *sorted(bundle_root.rglob("*"), key=lambda p: p.as_posix())]
                for path in paths:
                    relative = path.relative_to(bundle_root.parent)
                    info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    info.mode = 0o755 if path.is_dir() else normalized_mode(path)
                    if path.is_file():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def build_zip(bundle_root: Path, destination: Path, epoch: int) -> None:
    date_time = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(bundle_root.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle_root.parent).as_posix()
            info = zipfile.ZipInfo(relative, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = normalized_mode(path) << 16
            archive.writestr(info, path.read_bytes())


def build_release(
    source_root: Path,
    output_dir: Path,
    *,
    version: str,
    commit: str,
    epoch: int,
    docker_platforms: list[str],
) -> list[Path]:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise ValueError("commit must be a hexadecimal Git commit identifier")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_stem = f"docling-service-{version}"
    with tempfile.TemporaryDirectory(prefix="docling-service-release-") as directory:
        bundle_root = Path(directory) / archive_stem
        bundle_root.mkdir()
        copy_payload(source_root, bundle_root)
        (bundle_root / "VERSION").write_text(version + "\n", encoding="utf-8")
        write_manifest(
            bundle_root,
            version=version,
            commit=commit,
            epoch=epoch,
            docker_platforms=docker_platforms,
        )
        missing = sorted(path.as_posix() for path in REQUIRED_BUNDLE_FILES if not (bundle_root / path).is_file())
        if missing:
            raise RuntimeError(f"release bundle is incomplete: {missing}")
        tar_path = output_dir / f"{archive_stem}.tar.gz"
        zip_path = output_dir / f"{archive_stem}.zip"
        build_tar(bundle_root, tar_path, epoch)
        build_zip(bundle_root, zip_path, epoch)
    checksums_path = output_dir / "SHA256SUMS"
    checksums_path.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in (tar_path, zip_path)),
        encoding="utf-8",
    )
    sidecars: list[Path] = []
    for path in (tar_path, zip_path):
        sidecar = output_dir / f"{path.name}.sha256"
        sidecar.write_text(f"{sha256(path)}  {path.name}\n", encoding="utf-8")
        sidecars.append(sidecar)
    return [tar_path, zip_path, checksums_path, *sidecars]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=repository_root())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit")
    parser.add_argument(
        "--epoch",
        type=int,
        default=int(os.getenv("SOURCE_DATE_EPOCH", "0")) or None,
    )
    parser.add_argument(
        "--docker-platform",
        action="append",
        dest="docker_platforms",
        default=[],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source_root.resolve()
    commit = args.commit or git_commit(source_root)
    epoch = args.epoch
    if epoch is None:
        epoch = int(
            subprocess.check_output(
                ["git", "show", "-s", "--format=%ct", commit],
                cwd=source_root,
                text=True,
            ).strip()
        )
    platforms = args.docker_platforms or ["linux/amd64", "linux/arm64"]
    outputs = build_release(
        source_root,
        args.output_dir.resolve(),
        version=args.version,
        commit=commit,
        epoch=epoch,
        docker_platforms=platforms,
    )
    print(json.dumps({"ok": True, "outputs": [str(path) for path in outputs]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
