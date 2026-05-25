"""CLI for bounded MinerU VLM+MLX candidate probes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluation import run_page_probe, write_probe_result
from .model_registry import registry_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="MinerU local VLM+MLX candidate utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    registry_parser = subparsers.add_parser("registry", help="Print local model registry health")
    registry_parser.add_argument("--model-root", default="/Users/zeyuan/.cache/mineru/models")

    probe_parser = subparsers.add_parser("page-probe", help="Run a bounded single-page MinerU VLM+MLX probe")
    probe_parser.add_argument("--pdf", required=True)
    probe_parser.add_argument("--page", type=int, default=1)
    probe_parser.add_argument("--model-path", required=True)
    probe_parser.add_argument("--output-json", required=True)

    args = parser.parse_args()
    if args.command == "registry":
        print(json.dumps(registry_snapshot(Path(args.model_root)), indent=2, ensure_ascii=False))
        return 0
    if args.command == "page-probe":
        result = run_page_probe(
            pdf_path=Path(args.pdf).expanduser().resolve(),
            page_number=args.page,
            model_path=Path(args.model_path).expanduser().resolve(),
        )
        write_probe_result(Path(args.output_json).expanduser().resolve(), result)
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
