#!/usr/bin/env python3
"""Build deterministic, self-contained Docling Service release archives."""

from __future__ import annotations

import argparse
import ast
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

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.9 compatibility.
    tomllib = None


VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
VERSION_INLINE_RE = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+)$")
COMPOSE_RELEASE_IMAGE_SERVICES = {"api", "backend", "formula"}
COMPOSE_SERVICE_RE = re.compile(r"^  (?P<service>[A-Za-z0-9_.-]+):\s*$")
COMPOSE_PROPERTY_RE = re.compile(r"^    (?P<key>[A-Za-z0-9_.-]+):.*$")
DOCKERFILE_RELEASE_RE = re.compile(r"^ARG\s+RELEASE_VERSION=(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
IMAGE_TEMPLATE_RE = re.compile(
    r'^    image: "\$\{DOCLING_IMAGE_NAMESPACE:-ghcr\.io/kumaxs\}/'
    r"local-ai-lab-docling-(?P<service>api|backend|formula):"
    r'\$\{DOCLING_VERSION:-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\}"$'
)
SOURCE_COMPOSE_IMAGE_RE = re.compile(
    r"^    image: local-ai-lab/docling-(?P<service>api|backend|formula):"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)
README_VERSION_MARKER = re.compile(
    r"^# Docling Service (?P<version>[0-9]+\.[0-9]+\.[0-9]+) distribution bundle$",
    re.MULTILINE,
)
README_SHA256_MARKER = re.compile(
    r"^shasum -a 256 -c docling-service-"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz\.sha256$",
    re.MULTILINE,
)
README_IMMUTABLE_MARKER = re.compile(
    r"^The script pulls the immutable "
    r"`(?P<version>[0-9]+\.[0-9]+\.[0-9]+)` "
    r"image tags from GitHub Container$",
    re.MULTILINE,
)
INSTALLER_VERSION_MARKER = re.compile(
    r'(?:^|\n)print "Installed docling-service '
    r'(?P<version>[0-9]+\.[0-9]+\.[0-9]+) into \$\{VENV_DIR\}"\n'
    r'print "Start it with: \$\{SCRIPT_DIR\}/start\.sh"\s*\Z'
)
INSTALLER_STATUS_LINE_RE = re.compile(
    r'^print "Installed docling-service '
    r'(?P<version>[0-9]+\.[0-9]+\.[0-9]+) into \$\{VENV_DIR\}"$',
    re.MULTILINE,
)
TOML_SECTION_RE = re.compile(
    r"^\[(?P<section>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\]$"
)
TOML_ASSIGNMENT_RE = re.compile(
    r"^(?P<key>[A-Za-z0-9_-]+)\s*=\s*(?P<value>.*)$"
)
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


def _parse_limited_toml_string(
    raw_value: str, path: Path | str, line_number: int
) -> str:
    if len(raw_value) < 2 or raw_value[0] != raw_value[-1]:
        raise ValueError(f"unsupported TOML string in {path}:{line_number}")
    if raw_value[0] == '"':
        if r"\/" in raw_value:
            raise ValueError(f"unsupported TOML escape in {path}:{line_number}")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid TOML basic string in {path}:{line_number}"
            ) from exc
        if not isinstance(value, str):
            raise ValueError(f"invalid TOML basic string in {path}:{line_number}")
        return value
    if raw_value[0] == "'":
        body = raw_value[1:-1]
        if "'" in body or any(ord(char) < 0x20 and char != "\t" for char in body):
            raise ValueError(
                f"invalid TOML literal string in {path}:{line_number}"
            )
        return body
    raise ValueError(f"only TOML strings are supported in {path}:{line_number}")


def _parse_limited_toml_array(
    raw_value: str, path: Path | str, line_number: int
) -> list[str]:
    if len(raw_value) < 2 or raw_value[0] != "[" or raw_value[-1] != "]":
        raise ValueError(f"invalid TOML array in {path}:{line_number}")
    values: list[str] = []
    index = 1
    end = len(raw_value) - 1
    while True:
        while index < end and raw_value[index].isspace():
            index += 1
        if index == end:
            return values
        quote = raw_value[index]
        if quote not in {'"', "'"}:
            raise ValueError(
                f"only string arrays are supported in {path}:{line_number}"
            )
        start = index
        index += 1
        while index < end:
            char = raw_value[index]
            if quote == '"' and char == "\\":
                index += 2
                continue
            index += 1
            if char == quote:
                break
        else:
            raise ValueError(f"unterminated TOML string in {path}:{line_number}")
        values.append(
            _parse_limited_toml_string(
                raw_value[start:index], path, line_number
            )
        )
        while index < end and raw_value[index].isspace():
            index += 1
        if index == end:
            return values
        if raw_value[index] != ",":
            raise ValueError(f"invalid TOML array separator in {path}:{line_number}")
        index += 1


def _parse_limited_toml(text: str, path: Path | str) -> dict[str, dict[str, object]]:
    """Parse the string/array-only TOML subset used by this pyproject on 3.9."""
    tables: dict[str, dict[str, object]] = {}
    current_table: str | None = None
    array_key: str | None = None
    array_lines: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if array_key is not None:
            if line == "]":
                value = _parse_limited_toml_array(
                    "[\n" + "\n".join(array_lines) + "\n]",
                    path,
                    line_number,
                )
                tables[current_table or ""][array_key] = value
                array_key = None
                array_lines = []
            elif line and not line.startswith("#"):
                array_lines.append(line)
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section_match = TOML_SECTION_RE.fullmatch(line)
            if section_match is None:
                raise ValueError(f"unsupported TOML section in {path}:{line_number}")
            current_table = section_match.group("section")
            if current_table in tables:
                raise ValueError(
                    f"duplicate TOML section {current_table} in {path}:{line_number}"
                )
            tables[current_table] = {}
            continue
        assignment = TOML_ASSIGNMENT_RE.fullmatch(line)
        if assignment is None:
            raise ValueError(f"invalid TOML assignment in {path}:{line_number}")
        table = tables.setdefault(current_table or "", {})
        key = assignment.group("key")
        if key in table:
            raise ValueError(f"duplicate TOML key {key} in {path}:{line_number}")
        raw_value = assignment.group("value").strip()
        if not raw_value:
            raise ValueError(f"missing TOML value in {path}:{line_number}")
        if raw_value == "[":
            array_key = key
            array_lines = []
            continue
        if raw_value.startswith("["):
            value: object = _parse_limited_toml_array(
                raw_value, path, line_number
            )
        else:
            value = _parse_limited_toml_string(raw_value, path, line_number)
        table[key] = value
    if array_key is not None:
        raise ValueError(f"unterminated TOML array for {array_key} in {path}")
    return tables


def _extract_toml_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        payload = tomllib.loads(text)
    else:
        payload = _parse_limited_toml(text, path)
    project = payload.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_INLINE_RE.fullmatch(version):
        raise ValueError(f"invalid or missing project.version in {path}: {version!r}")
    return version


def _extract_ast_version(path: Path, constant: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != constant:
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{constant} must be a literal string in {path}") from exc
            if not isinstance(value, str) or not VERSION_INLINE_RE.fullmatch(value):
                raise ValueError(f"invalid {constant} in {path}: {value!r}")
            matches.append(value)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {constant} assignment in {path}; found {len(matches)}"
        )
    return matches[0]


def _extract_compose_image_versions(
    path: Path, image_pattern: re.Pattern[str]
) -> dict[str, str]:
    versions: dict[str, str] = {}
    declared_services: set[str] = set()
    current_service: str | None = None
    services_blocks = 0
    inside_services = False
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if line and not line[0].isspace() and not line.startswith("#"):
            current_service = None
            if line.startswith("services:"):
                if line != "services:":
                    raise ValueError(
                        f"unsupported root services declaration in {path}:{line_number}"
                    )
                services_blocks += 1
                if services_blocks > 1:
                    raise ValueError(f"duplicate root services declaration in {path}")
                inside_services = True
            else:
                inside_services = False
            continue
        if not inside_services:
            continue
        if line.startswith("  ") and not line.startswith("    "):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            service_match = COMPOSE_SERVICE_RE.fullmatch(line)
            if service_match is None:
                raise ValueError(
                    f"unsupported service declaration in {path}:{line_number}: "
                    f"{line.strip()}"
                )
            service = service_match.group("service")
            if service not in COMPOSE_RELEASE_IMAGE_SERVICES:
                raise ValueError(f"unexpected service {service} in {path}:{line_number}")
            if service in declared_services:
                raise ValueError(
                    f"duplicate service declaration for {service} in {path}"
                )
            declared_services.add(service)
            current_service = service
            continue
        if not line.startswith("    ") or line.startswith("      "):
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        property_match = COMPOSE_PROPERTY_RE.fullmatch(line)
        if property_match is None or current_service is None:
            raise ValueError(
                f"unsupported service property in {path}:{line_number}: "
                f"{line.strip()}"
            )
        if property_match.group("key") != "image":
            continue
        if current_service in versions:
            raise ValueError(f"duplicate image for {current_service} in {path}")
        match = image_pattern.fullmatch(line)
        if match is None or match.group("service") != current_service:
            raise ValueError(
                f"invalid image for {current_service} in {path}:{line_number}: "
                f"{line.strip()}"
            )
        versions[current_service] = match.group("version")
    if services_blocks != 1:
        raise ValueError(f"expected exactly one root services declaration in {path}")
    missing_declarations = COMPOSE_RELEASE_IMAGE_SERVICES - declared_services
    if missing_declarations:
        raise ValueError(
            f"compose declarations missing services in {path}: "
            f"{sorted(missing_declarations)}"
        )
    missing = COMPOSE_RELEASE_IMAGE_SERVICES - versions.keys()
    if missing:
        raise ValueError(f"compose images missing services in {path}: {sorted(missing)}")
    return versions


def _extract_compose_default_versions(path: Path) -> dict[str, str]:
    return _extract_compose_image_versions(path, IMAGE_TEMPLATE_RE)


def _extract_dockerfile_arg_version(path: Path) -> str:
    versions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("ARG RELEASE_VERSION"):
            continue
        match = DOCKERFILE_RELEASE_RE.fullmatch(stripped)
        if match is None:
            raise ValueError(f"invalid RELEASE_VERSION ARG in {path}: {stripped}")
        versions.append(match.group("version"))
    if len(versions) != 1:
        raise ValueError(
            f"expected exactly one RELEASE_VERSION ARG in {path}; found {len(versions)}"
        )
    return versions[0]


def _extract_compose_local_versions(path: Path) -> dict[str, str]:
    return _extract_compose_image_versions(path, SOURCE_COMPOSE_IMAGE_RE)


def _extract_bundle_readme_versions(path: Path) -> set[str]:
    text = _strip_html_comments(path.read_text(encoding="utf-8"), path)
    versions: set[str] = set()
    for matcher in (README_VERSION_MARKER, README_SHA256_MARKER, README_IMMUTABLE_MARKER):
        matches = {match.group("version") for match in matcher.finditer(text)}
        if not matches:
            raise ValueError(f"bundle README lacks version metadata in {path}")
        if len(matches) != 1:
            raise ValueError(
                f"bundle README contains conflicting version metadata in {path}: "
                f"{sorted(matches)}"
            )
        versions.update(matches)
    return versions


def _strip_html_comments(text: str, path: Path | str) -> str:
    visible: list[str] = []
    cursor = 0
    while True:
        opening = text.find("<!--", cursor)
        closing = text.find("-->", cursor)
        if closing >= 0 and (opening < 0 or closing < opening):
            raise ValueError(f"bundle README has an unmatched HTML comment close in {path}")
        if opening < 0:
            visible.append(text[cursor:])
            return "".join(visible)
        visible.append(text[cursor:opening])
        closing = text.find("-->", opening + 4)
        if closing < 0:
            raise ValueError(f"bundle README has an unterminated HTML comment in {path}")
        cursor = closing + 3


def _extract_macos_installer_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    status_lines = [
        match.group("version")
        for match in INSTALLER_STATUS_LINE_RE.finditer(text)
    ]
    final_match = INSTALLER_VERSION_MARKER.search(text)
    if (
        final_match is None
        or len(status_lines) != 1
        or status_lines[0] != final_match.group("version")
    ):
        raise ValueError(f"macOS installer version output is missing in {path}")
    return final_match.group("version")


def _validate_requested_version(source_root: Path, requested_version: str) -> str:
    service_root = source_root / "services/docling-service"
    project_version = _extract_toml_version(service_root / "pyproject.toml")
    release_version = _extract_ast_version(service_root / "docling_service/release.py", "RELEASE_VERSION")
    formula_version = _extract_ast_version(
        service_root / "docling_service/formula_api.py", "FORMULA_SERVICE_VERSION"
    )
    init_version = _extract_ast_version(service_root / "docling_service/__init__.py", "__version__")
    compose_versions = _extract_compose_default_versions(
        service_root / "deploy/docker/compose.release.yaml"
    )
    source_compose_versions = _extract_compose_local_versions(
        service_root / "deploy/docker/compose.yaml"
    )
    dockerfile_versions = {
        "api": _extract_dockerfile_arg_version(
            service_root / "deploy/docker/Dockerfile.api"
        ),
        "backend": _extract_dockerfile_arg_version(
            service_root / "deploy/docker/Dockerfile.backend"
        ),
        "formula": _extract_dockerfile_arg_version(
            service_root / "deploy/docker/Dockerfile.formula"
        ),
    }
    compose_unique = set(compose_versions.values())
    if len(compose_unique) != 1:
        raise ValueError(
            "compose.release.yaml default image tags are inconsistent across services"
        )
    compose_version = next(iter(compose_unique))
    readme_versions = _extract_bundle_readme_versions(
        service_root / "release/BUNDLE_README.md"
    )
    installer_version = _extract_macos_installer_version(
        service_root / "deploy/macos/install.sh"
    )
    all_versions = {
        project_version,
        release_version,
        formula_version,
        compose_version,
        installer_version,
        init_version,
        *source_compose_versions.values(),
        *dockerfile_versions.values(),
        *readme_versions,
    }
    if len(all_versions) != 1:
        raise ValueError(
            "release version is not consistent across source files: "
            f"project={project_version}, release={release_version}, "
            f"formula={formula_version}, compose={compose_version}, "
            f"source_compose={source_compose_versions}, "
            f"dockerfiles={dockerfile_versions}, "
            f"init={init_version}, "
            f"readme={sorted(readme_versions)}, installer={installer_version}"
        )
    resolved_version = next(iter(all_versions))
    if requested_version != resolved_version:
        raise ValueError(
            "requested version does not match source release version: "
            f"requested={requested_version}, source={resolved_version}"
        )
    return resolved_version


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
    _validate_requested_version(source_root, version)
    archive_stem = f"docling-service-{version}"
    with tempfile.TemporaryDirectory(prefix="docling-service-release-") as directory:
        bundle_root = Path(directory) / archive_stem
        bundle_root.mkdir()
        copy_payload(source_root, bundle_root)
        _validate_requested_version(bundle_root, version)
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
        output_dir.mkdir(parents=True, exist_ok=True)
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
