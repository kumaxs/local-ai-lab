#!/usr/bin/env python3
"""Unified inbox processor for n8n-triggered PDF intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process an inbox directory with hash-based de-duplication."
    )
    parser.add_argument("--input-dir", required=True, help="Directory of downloaded files")
    parser.add_argument("--output-dir", required=True, help="Directory for outputs")
    parser.add_argument("--state", required=True, help="processed_index.json path")
    return parser.parse_args()


def resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_source_type(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(512)
    stripped = head.lstrip()
    lowered = stripped[:128].lower()
    if stripped.startswith(b"%PDF"):
        return "pdf"
    if lowered.startswith((b"<!doctype", b"<html", b"<!doc")):
        return "html"
    return "unsupported"


def safe_slug(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
    return (slug or "file")[:80].strip("-") or "file"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_state(path: Path) -> dict[str, Any]:
    state = load_json(path)
    processed = state.get("processed")
    if isinstance(processed, dict):
        return state
    if isinstance(state, dict) and all(
        isinstance(value, dict) and "sha256" in value for value in state.values()
    ):
        return {"processed": state}
    return {"processed": {}}


def output_paths(output_dir: Path, source: Path, digest: str) -> dict[str, Path]:
    base = f"{safe_slug(source.stem)}.{digest[:8]}"
    return {
        "txt": output_dir / f"{base}.raw.txt",
        "md": output_dir / f"{base}.extract.md",
        "meta": output_dir / f"{base}.meta.json",
    }


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def ensure_pdf_meta(meta_path: Path) -> dict[str, Any]:
    meta = load_json(meta_path)
    meta.setdefault("source_type", "pdf")
    meta.setdefault("source_status", "pdf_extracted")
    meta.setdefault("needs_pdf", False)
    write_json(meta_path, meta)
    return meta


def write_failure_meta(
    meta_path: Path,
    source: Path,
    source_type: str,
    exit_code: int,
    stderr: str,
) -> dict[str, Any]:
    meta = {
        "source": str(source.resolve()),
        "source_type": source_type,
        "source_status": "extraction_failed",
        "needs_pdf": source_type != "pdf",
        "needs_ocr": None,
        "extraction_quality": "failed",
        "quality_flags": ["processing_failed"],
        "process_exit_code": exit_code,
        "warnings": [stderr] if stderr else [],
    }
    write_json(meta_path, meta)
    return meta


def process_file(
    source: Path,
    output_dir: Path,
    scripts_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    digest = sha256_file(source)
    source_type = detect_source_type(source)
    paths = output_paths(output_dir, source, digest)

    if source_type == "pdf":
        command = [
            sys.executable,
            str(scripts_dir / "pdf_extract.py"),
            str(source),
            "--txt",
            str(paths["txt"]),
            "--md",
            str(paths["md"]),
            "--meta",
            str(paths["meta"]),
            "--layout",
            "auto",
        ]
    else:
        command = [
            sys.executable,
            str(scripts_dir / "intake_detect.py"),
            str(source),
            "--txt",
            str(paths["txt"]),
            "--md",
            str(paths["md"]),
            "--meta",
            str(paths["meta"]),
            "--layout",
            "auto",
        ]

    result = run_command(command)
    if result.returncode == 0 and paths["meta"].exists():
        meta = ensure_pdf_meta(paths["meta"]) if source_type == "pdf" else load_json(paths["meta"])
    else:
        meta = write_failure_meta(
            paths["meta"], source, source_type, result.returncode, result.stderr.strip()
        )

    now = datetime.now(timezone.utc).isoformat()
    record = {
        "sha256": digest,
        "original_filename": source.name,
        "source_path": str(source.resolve()),
        "processed_at": now,
        "source_type": meta.get("source_type", source_type),
        "source_status": meta.get("source_status"),
        "needs_pdf": meta.get("needs_pdf"),
        "needs_ocr": meta.get("needs_ocr"),
        "extraction_quality": meta.get("extraction_quality"),
        "layout_detected": meta.get("layout_detected"),
        "output_paths": {
            "txt": str(paths["txt"]),
            "md": str(paths["md"]),
            "meta": str(paths["meta"]),
        },
        "process_exit_code": result.returncode,
    }
    summary = {
        "filename": source.name,
        "sha256": digest,
        "status": "processed",
        "exit_code": result.returncode,
        "source_type": record["source_type"],
        "source_status": record["source_status"],
        "needs_pdf": record["needs_pdf"],
        "needs_ocr": record["needs_ocr"],
        "extraction_quality": record["extraction_quality"],
        "layout_detected": record["layout_detected"],
        "meta": str(paths["meta"]),
    }
    return record, summary


def make_run_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Inbox Processing Summary",
        "",
        "| File | Status | Type | Source Status | Needs PDF | Needs OCR | Quality | Layout | Exit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {filename} | {status} | {source_type} | {source_status} | {needs_pdf} | "
            "{needs_ocr} | {quality} | {layout} | {exit_code} |".format(
                filename=row["filename"],
                status=row["status"],
                source_type=row.get("source_type"),
                source_status=row.get("source_status"),
                needs_pdf=row.get("needs_pdf"),
                needs_ocr=row.get("needs_ocr"),
                quality=row.get("extraction_quality"),
                layout=row.get("layout_detected"),
                exit_code=row.get("exit_code"),
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    scripts_dir = Path(__file__).resolve().parent
    input_dir = resolve_path(project_root, args.input_dir)
    output_dir = resolve_path(project_root, args.output_dir)
    state_path = resolve_path(project_root, args.state)

    if not input_dir.exists():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not input_dir.is_dir():
        print(f"Input path is not a directory: {input_dir}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    processed: dict[str, Any] = state["processed"]
    rows: list[dict[str, Any]] = []

    for source in sorted(path for path in input_dir.iterdir() if path.is_file()):
        if source.name.startswith("."):
            continue
        digest = sha256_file(source)
        if digest in processed:
            record = processed[digest]
            rows.append(
                {
                    "filename": source.name,
                    "sha256": digest,
                    "status": "skipped_duplicate",
                    "exit_code": 0,
                    "source_type": record.get("source_type"),
                    "source_status": record.get("source_status"),
                    "needs_pdf": record.get("needs_pdf"),
                    "needs_ocr": record.get("needs_ocr"),
                    "extraction_quality": record.get("extraction_quality"),
                    "layout_detected": record.get("layout_detected"),
                    "meta": record.get("output_paths", {}).get("meta"),
                }
            )
            continue

        record, summary = process_file(source, output_dir, scripts_dir)
        processed[digest] = record
        rows.append(summary)

    state["processed"] = processed
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(state_path, state)

    run_payload = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "state": str(state_path),
        "processed_count": sum(1 for row in rows if row["status"] == "processed"),
        "skipped_count": sum(1 for row in rows if row["status"] == "skipped_duplicate"),
        "results": rows,
    }
    write_json(output_dir / "run_summary.json", run_payload)
    (output_dir / "run_summary.md").write_text(make_run_markdown(rows), encoding="utf-8")

    print(
        "processed={processed_count} skipped={skipped_count} total={total} output_dir={output_dir}".format(
            processed_count=run_payload["processed_count"],
            skipped_count=run_payload["skipped_count"],
            total=len(rows),
            output_dir=output_dir,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
