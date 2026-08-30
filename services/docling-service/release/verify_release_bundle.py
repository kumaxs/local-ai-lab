#!/usr/bin/env python3
"""Verify Docling Service release archives without extracting them."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


REQUIRED_FILES = {
    "README.md",
    "VERSION",
    "RELEASE_MANIFEST.json",
    "install-macos.sh",
    "docker-up.sh",
    "docker-down.sh",
    "services/docling-service/deploy/docker/compose.release.yaml",
    "services/docling-service/deploy/macos/install.sh",
    "services/docling-service/docling_service/release.py",
    "services/docling-service/docling_service/ui/index.html",
    "services/docling-service/docling_service/ui/main.js",
    "services/docling-service/docling_service/ui/styles.css",
    "docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py",
    "docs/integrations/docling-serve-quality-parity/pdf_structure_inventory.py",
    "docs/integrations/docling-serve-quality-parity/region_quality_gate.py",
}
BANNED_PARTS = {".git", ".runtime", ".venv", "__pycache__", "reports", "inputs", "outputs"}
BANNED_SUFFIXES = {".pdf", ".log", ".pyc", ".pyo"}


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relative(name: str, root: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    if not path.parts or path.parts[0] != root:
        raise ValueError(f"archive contains more than one root: {name}")
    if len(path.parts) == 1:
        return None
    return PurePosixPath(*path.parts[1:]).as_posix()


def read_tar(path: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        roots = {PurePosixPath(member.name).parts[0] for member in members if member.name}
        if len(roots) != 1:
            raise ValueError("archive must have exactly one root directory")
        root = roots.pop()
        for member in members:
            relative = safe_relative(member.name, root)
            if member.issym() or member.islnk():
                raise ValueError(f"links are not allowed in release archives: {member.name}")
            if relative is None or member.isdir():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"unable to read archive member: {member.name}")
            payload[relative] = handle.read()
    return payload


def read_zip(path: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name and not name.endswith("/")]
        roots = {PurePosixPath(name).parts[0] for name in names}
        if len(roots) != 1:
            raise ValueError("archive must have exactly one root directory")
        root = roots.pop()
        for name in names:
            relative = safe_relative(name, root)
            if relative is not None:
                payload[relative] = archive.read(name)
    return payload


def verify_payload(payload: dict[str, bytes]) -> dict[str, object]:
    missing = sorted(REQUIRED_FILES - payload.keys())
    if missing:
        raise ValueError(f"required files are missing: {missing}")
    for name in payload:
        path = PurePosixPath(name)
        if any(part in BANNED_PARTS for part in path.parts):
            raise ValueError(f"runtime/development content is forbidden: {name}")
        if path.suffix.casefold() in BANNED_SUFFIXES:
            raise ValueError(f"forbidden file type in release: {name}")
    manifest = json.loads(payload["RELEASE_MANIFEST.json"])
    if manifest.get("schema_version") != 1 or manifest.get("product") != "docling-service":
        raise ValueError("release manifest identity is invalid")
    version = str(manifest.get("version") or "")
    if payload["VERSION"].decode("utf-8").strip() != version:
        raise ValueError("VERSION and release manifest disagree")
    expected_entries = manifest.get("files")
    if not isinstance(expected_entries, list):
        raise ValueError("release manifest files must be a list")
    expected_names: set[str] = set()
    for entry in expected_entries:
        if not isinstance(entry, dict):
            raise ValueError("release manifest file entry is invalid")
        name = str(entry.get("path") or "")
        expected_names.add(name)
        if name not in payload:
            raise ValueError(f"manifest file is missing from archive: {name}")
        data = payload[name]
        if digest_bytes(data) != entry.get("sha256") or len(data) != entry.get("size"):
            raise ValueError(f"manifest checksum or size mismatch: {name}")
    actual_names = set(payload) - {"RELEASE_MANIFEST.json"}
    if actual_names != expected_names:
        extras = sorted(actual_names - expected_names)
        omitted = sorted(expected_names - actual_names)
        raise ValueError(f"manifest coverage mismatch: extras={extras}, omitted={omitted}")
    return {
        "ok": True,
        "version": version,
        "git_commit": manifest.get("git_commit"),
        "files": len(payload),
        "docker_platforms": manifest.get("docker_platforms"),
    }


def verify_archive(path: Path) -> dict[str, object]:
    if path.name.endswith(".tar.gz"):
        payload = read_tar(path)
    elif path.suffix.casefold() == ".zip":
        payload = read_zip(path)
    else:
        raise ValueError("release archive must end in .tar.gz or .zip")
    return verify_payload(payload)


def verify_checksum_file(checksums: Path, archives: list[Path]) -> None:
    expected: dict[str, str] = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        expected[name.strip()] = digest
    for archive in archives:
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected.get(archive.name) != actual:
            raise ValueError(f"SHA256SUMS mismatch: {archive.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--checksums", type=Path)
    args = parser.parse_args()
    if args.checksums:
        verify_checksum_file(args.checksums, args.archives)
    results = [verify_archive(path) for path in args.archives]
    print(json.dumps({"ok": True, "archives": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
