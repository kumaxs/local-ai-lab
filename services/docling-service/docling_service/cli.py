"""Local CLI for the docling-service skeleton."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .contract import (
    DEFAULT_TIMEOUT_SECONDS,
    STATUS_FAILED_CONVERSION,
    STATUS_FAILED_INTERNAL,
)
from .converter import docling_convert, placeholder_convert
from .docling_adapter import DoclingAdapterError
from .validate import validate_request
from .writer import utc_now_iso


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal docling-service local CLI skeleton.")
    parser.add_argument("--job-uuid", required=True)
    parser.add_argument("--input-file-path", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--original-name")
    parser.add_argument("--source-name")
    parser.add_argument("--image-export-mode", default="referenced")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output-root")
    parser.add_argument("--converter", choices=("placeholder", "docling"), default="placeholder")
    return parser


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    validation = validate_request(
        job_uuid=args.job_uuid,
        input_file_path=args.input_file_path,
        image_export_mode=args.image_export_mode,
        timeout_seconds=args.timeout_seconds,
    )
    if not validation.ok:
        emit(
            {
                "ok": False,
                "status": validation.status,
                "job_uuid": args.job_uuid,
                "output_dir": None,
                "metadata_path": None,
                "status_path": None,
                "error": {
                    "code": validation.error_code,
                    "message": validation.error_message,
                },
            }
        )
        return 2

    started_monotonic = time.monotonic()
    started_at = utc_now_iso()
    try:
        converter = placeholder_convert if args.converter == "placeholder" else docling_convert
        result = converter(
            job_uuid=args.job_uuid,
            input_file_path=validation.input_file_path,
            output_root=args.output_root,
            display_name=args.display_name,
            original_name=args.original_name,
            source_name=args.source_name,
            image_export_mode=validation.image_export_mode,
            started_at=started_at,
            finished_at=utc_now_iso(),
            duration_seconds=round(time.monotonic() - started_monotonic, 6),
        )
    except DoclingAdapterError as exc:
        emit(
            {
                "ok": False,
                "status": STATUS_FAILED_CONVERSION,
                "job_uuid": args.job_uuid,
                "output_dir": None,
                "metadata_path": None,
                "status_path": None,
                "error": {
                    "code": "docling_conversion_unavailable",
                    "message": str(exc),
                },
            }
        )
        return 3
    except Exception:
        emit(
            {
                "ok": False,
                "status": STATUS_FAILED_INTERNAL,
                "job_uuid": args.job_uuid,
                "output_dir": None,
                "metadata_path": None,
                "status_path": None,
                "error": {
                    "code": "internal_error",
                    "message": "internal error while writing conversion outputs",
                },
            }
        )
        return 1

    emit(
        {
            "ok": True,
            "status": result["status"]["status"],
            "job_uuid": args.job_uuid,
            "output_dir": str(result["output_dir"]),
            "metadata_path": str(result["metadata_path"]),
            "status_path": str(result["status_path"]),
            "error": None,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
