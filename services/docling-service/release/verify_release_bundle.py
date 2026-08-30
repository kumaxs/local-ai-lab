#!/usr/bin/env python3
"""Verify Docling Service release archives without extracting them."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.9 compatibility.
    tomllib = None


REQUIRED_FILES = {
    "README.md",
    "VERSION",
    "RELEASE_MANIFEST.json",
    "install-macos.sh",
    "docker-up.sh",
    "docker-down.sh",
    "services/docling-service/deploy/docker/compose.release.yaml",
    "services/docling-service/deploy/docker/compose.yaml",
    "services/docling-service/deploy/docker/Dockerfile.api",
    "services/docling-service/deploy/docker/Dockerfile.backend",
    "services/docling-service/deploy/docker/Dockerfile.formula",
    "services/docling-service/deploy/docker/backend-constraints.txt",
    "services/docling-service/deploy/macos/constraints.txt",
    "services/docling-service/deploy/macos/install.sh",
    "services/docling-service/deploy/macos/lifecycle.py",
    "services/docling-service/deploy/macos/logging_wrapper.py",
    "services/docling-service/deploy/macos/run-api.sh",
    "services/docling-service/deploy/macos/run-backend.sh",
    "services/docling-service/deploy/macos/runtime.txt",
    "services/docling-service/deploy/macos/start.sh",
    "services/docling-service/deploy/macos/status.sh",
    "services/docling-service/deploy/macos/stop.sh",
    "services/docling-service/pyproject.toml",
    "services/docling-service/docling_service/__init__.py",
    "services/docling-service/docling_service/formula_api.py",
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
VERSION_INLINE_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMPOSE_RELEASE_IMAGE_SERVICES = {"api", "backend", "formula"}
COMPOSE_SERVICE_RE = re.compile(r"^  (?P<service>[A-Za-z0-9_.-]+):\s*$")
COMPOSE_PROPERTY_RE = re.compile(r"^    (?P<key>[A-Za-z0-9_.-]+):.*$")
IMAGE_TEMPLATE_RE = re.compile(
    r'^    image: "\$\{DOCLING_IMAGE_NAMESPACE:-ghcr\.io/kumaxs\}/'
    r"local-ai-lab-docling-(?P<service>api|backend|formula):"
    r'\$\{DOCLING_VERSION:-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\}"$'
)
DOCKERFILE_RELEASE_RE = re.compile(
    r"^ARG\s+RELEASE_VERSION=(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)
SOURCE_COMPOSE_IMAGE_RE = re.compile(
    r"^    image: local-ai-lab/docling-(?P<service>api|backend|formula):"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)
MANIFEST_IMAGE_RE = re.compile(
    r"^ghcr\.io/kumaxs/local-ai-lab-docling-(?P<service>api|backend|formula):(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
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


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key in release manifest: {key}")
        payload[key] = value
    return payload


def _extract_text(payload: dict[str, bytes], path: str) -> str:
    try:
        return payload[path].decode("utf-8")
    except KeyError as exc:
        raise ValueError(f"required file is missing: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"file is not valid utf-8: {path}") from exc


def _parse_limited_toml_string(
    raw_value: str, path: bytes | str, line_number: int
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
    raw_value: str, path: bytes | str, line_number: int
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


def _parse_limited_toml(text: str, path: bytes | str) -> dict[str, dict[str, object]]:
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


def _extract_toml_version(
    path: bytes | str, *, content: bytes | None = None
) -> str:
    text = content.decode("utf-8") if content is not None else Path(path).read_text(encoding="utf-8")
    if tomllib is not None:
        payload = tomllib.loads(text)
    else:
        payload = _parse_limited_toml(text, path)
    project = payload.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_INLINE_RE.fullmatch(version):
        raise ValueError(f"invalid or missing project.version in {path}: {version!r}")
    return version


def _extract_ast_version(path: bytes | str, constant: str, *, content: bytes | None = None) -> str:
    source = content.decode("utf-8") if content is not None else Path(path).read_text(encoding="utf-8")
    matches: list[str] = []
    for node in ast.parse(source).body:
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
    path: str,
    image_pattern: re.Pattern[str],
    *,
    content: bytes | None = None,
) -> dict[str, str]:
    text = content.decode("utf-8") if content is not None else Path(path).read_text(encoding="utf-8")
    versions: dict[str, str] = {}
    declared_services: set[str] = set()
    current_service: str | None = None
    services_blocks = 0
    inside_services = False
    for line_number, line in enumerate(text.splitlines(), start=1):
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


def _extract_compose_default_versions(path: str, *, content: bytes | None = None) -> dict[str, str]:
    return _extract_compose_image_versions(
        path, IMAGE_TEMPLATE_RE, content=content
    )


def _extract_dockerfile_arg_version(
    path: bytes | str, *, content: bytes | None = None
) -> str:
    text = content.decode("utf-8") if content is not None else Path(path).read_text(encoding="utf-8")
    versions: list[str] = []
    for line in text.splitlines():
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


def _extract_compose_source_versions(
    path: bytes | str, *, content: bytes | None = None
) -> dict[str, str]:
    return _extract_compose_image_versions(
        str(path), SOURCE_COMPOSE_IMAGE_RE, content=content
    )


def _extract_bundle_readme_versions(text: str, path: str) -> set[str]:
    text = _strip_html_comments(text, path)
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


def _strip_html_comments(text: str, path: str) -> str:
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


def _extract_macos_installer_version(text: str, path: str) -> str:
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


def _validate_version_consistency(version: str, manifest: dict[str, object], payload: dict[str, bytes]) -> None:
    if version != payload["VERSION"].decode("utf-8").strip():
        raise ValueError("VERSION and release manifest disagree")

    readme_versions = _extract_bundle_readme_versions(_extract_text(payload, "README.md"), "README.md")
    installer_version = _extract_macos_installer_version(
        _extract_text(payload, "services/docling-service/deploy/macos/install.sh"),
        "services/docling-service/deploy/macos/install.sh",
    )
    compose_versions = _extract_compose_default_versions(
        "services/docling-service/deploy/docker/compose.release.yaml",
        content=payload["services/docling-service/deploy/docker/compose.release.yaml"],
    )
    project_version = _extract_toml_version(
        "services/docling-service/pyproject.toml",
        content=payload["services/docling-service/pyproject.toml"],
    )
    release_version = _extract_ast_version(
        "services/docling-service/docling_service/release.py",
        "RELEASE_VERSION",
        content=payload["services/docling-service/docling_service/release.py"],
    )
    formula_version = _extract_ast_version(
        "services/docling-service/docling_service/formula_api.py",
        "FORMULA_SERVICE_VERSION",
        content=payload["services/docling-service/docling_service/formula_api.py"],
    )
    init_version = _extract_ast_version(
        "services/docling-service/docling_service/__init__.py",
        "__version__",
        content=payload["services/docling-service/docling_service/__init__.py"],
    )
    source_compose_versions = _extract_compose_source_versions(
        "services/docling-service/deploy/docker/compose.yaml",
        content=payload[
            "services/docling-service/deploy/docker/compose.yaml"
        ],
    )
    dockerfile_versions = {
        "api": _extract_dockerfile_arg_version(
            "services/docling-service/deploy/docker/Dockerfile.api",
            content=payload["services/docling-service/deploy/docker/Dockerfile.api"],
        ),
        "backend": _extract_dockerfile_arg_version(
            "services/docling-service/deploy/docker/Dockerfile.backend",
            content=payload["services/docling-service/deploy/docker/Dockerfile.backend"],
        ),
        "formula": _extract_dockerfile_arg_version(
            "services/docling-service/deploy/docker/Dockerfile.formula",
            content=payload["services/docling-service/deploy/docker/Dockerfile.formula"],
        ),
    }

    manifest_images = manifest.get("docker_images")
    if not isinstance(manifest_images, dict):
        raise ValueError("manifest docker images field is invalid")
    if set(manifest_images) != COMPOSE_RELEASE_IMAGE_SERVICES:
        raise ValueError(
            "manifest docker image services mismatch: "
            f"{sorted(str(name) for name in manifest_images)}"
        )
    for service in COMPOSE_RELEASE_IMAGE_SERVICES:
        image = manifest_images[service]
        if not isinstance(image, str):
            raise ValueError(f"manifest docker image for {service} is missing")
        match = MANIFEST_IMAGE_RE.fullmatch(image)
        if not match:
            raise ValueError(f"manifest docker image format invalid for {service}: {image}")
        if match.group("service") != service:
            raise ValueError(
                f"manifest docker image service mismatch: {service}={image}"
            )
        image_version = match.group("version")
        if image_version != version:
            raise ValueError(
                f"manifest docker image version mismatch: {service}={image_version}, manifest={version}"
            )

    compose_unique = set(compose_versions.values())
    if len(compose_unique) != 1 or next(iter(compose_unique)) != version:
        raise ValueError(
            f"compose image defaults do not match manifest version {version}: {compose_versions}"
        )

    source_versions = {
        version,
        project_version,
        release_version,
        formula_version,
        init_version,
        installer_version,
        *readme_versions,
        *compose_versions.values(),
        *source_compose_versions.values(),
        *dockerfile_versions.values(),
    }
    if len(source_versions) != 1:
        raise ValueError(
            "version is inconsistent inside archive: "
            f"manifest={version}, project={project_version}, "
            f"release={release_version}, formula={formula_version}, "
            f"init={init_version}, "
            f"source_compose={source_compose_versions}, "
            f"dockerfiles={dockerfile_versions}, "
            f"installer={installer_version}, compose={sorted(set(compose_versions.values()))}, "
            f"readme={sorted(readme_versions)}"
        )


def safe_relative(name: str, root: str) -> str | None:
    if "\\" in name:
        raise ValueError(f"unsafe archive path separator: {name}")
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
            if relative in payload:
                raise ValueError(f"duplicate archive member: {member.name}")
            if not member.isfile():
                raise ValueError(
                    f"special files are not allowed in release archives: {member.name}"
                )
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"unable to read archive member: {member.name}")
            payload[relative] = handle.read()
    return payload


def read_zip(path: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        infos = [info for info in archive.infolist() if info.filename]
        names = [info.filename for info in infos]
        roots = {PurePosixPath(name).parts[0] for name in names}
        if len(roots) != 1:
            raise ValueError("archive must have exactly one root directory")
        root = roots.pop()
        for info in infos:
            relative = safe_relative(info.filename, root)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if file_type == stat.S_IFLNK:
                raise ValueError(
                    f"links are not allowed in release archives: {info.filename}"
                )
            if info.is_dir():
                if file_type not in (0, stat.S_IFDIR):
                    raise ValueError(
                        f"special files are not allowed in release archives: "
                        f"{info.filename}"
                    )
                continue
            if file_type not in (0, stat.S_IFREG):
                raise ValueError(
                    f"special files are not allowed in release archives: "
                    f"{info.filename}"
                )
            if relative is None:
                raise ValueError(f"archive root must be a directory: {info.filename}")
            if relative in payload:
                raise ValueError(f"duplicate archive member: {info.filename}")
            payload[relative] = archive.read(info)
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
    manifest = json.loads(
        payload["RELEASE_MANIFEST.json"],
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    if manifest.get("schema_version") != 1 or manifest.get("product") != "docling-service":
        raise ValueError("release manifest identity is invalid")
    version = str(manifest.get("version") or "")
    expected_entries = manifest.get("files")
    if not isinstance(expected_entries, list):
        raise ValueError("release manifest files must be a list")
    if payload["VERSION"].decode("utf-8").strip() != version:
        raise ValueError("VERSION and release manifest disagree")
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
    _validate_version_consistency(version, manifest, payload)
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
