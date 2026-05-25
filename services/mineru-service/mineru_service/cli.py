"""CLI for MinerU VLM+MLX candidate probes and review-output conversion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contract import convert_pdf_to_contract
from .evaluation import run_page_probe, write_probe_result
from .model_registry import default_models, registry_snapshot


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

    convert_parser = subparsers.add_parser("convert", help="Write Local AI Lab review contract outputs")
    convert_parser.add_argument("--pdf", required=True)
    convert_parser.add_argument("--output-dir", required=True)
    convert_parser.add_argument("--model-path")
    convert_parser.add_argument("--model-root", default="/Users/zeyuan/.cache/mineru/models")
    convert_parser.add_argument("--pages", help="Optional page range such as 1,3-4")
    convert_parser.add_argument("--render-zoom", type=float, default=2.0)

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
    if args.command == "convert":
        if args.model_path:
            model = default_models(Path(args.model_root))[0]
            model = type(model)(
                model_id=model.model_id,
                source_repo=model.source_repo,
                local_path=Path(args.model_path).expanduser().resolve(),
                quantization=model.quantization,
                expected_runtime=model.expected_runtime,
                required_packages=model.required_packages,
                health_check_files=model.health_check_files,
                cleanup_policy=model.cleanup_policy,
            )
        else:
            model = default_models(Path(args.model_root))[0]
        result = convert_pdf_to_contract(
            pdf_path=Path(args.pdf).expanduser().resolve(),
            output_dir=Path(args.output_dir).expanduser().resolve(),
            model=model,
            page_range=args.pages,
            render_zoom=args.render_zoom,
        )
        print(json.dumps({"metadata": result.metadata, "status": result.status}, indent=2, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
