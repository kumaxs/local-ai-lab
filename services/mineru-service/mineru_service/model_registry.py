"""Lightweight model registry helpers for local functional model candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MODEL_ROOT = Path("/Users/zeyuan/.cache/mineru/models")


@dataclass(frozen=True)
class LocalModel:
    model_id: str
    source_repo: str
    local_path: Path
    quantization: str
    expected_runtime: str
    required_packages: tuple[str, ...]
    health_check_files: tuple[str, ...]
    cleanup_policy: str

    @property
    def present(self) -> bool:
        if not self.local_path.exists():
            return False
        if any(self.local_path.glob("*.aria2")):
            return False
        return all((self.local_path / name).exists() for name in self.health_check_files)

    def to_dict(self) -> dict[str, Any]:
        size_bytes = directory_size(self.local_path) if self.local_path.exists() else 0
        return {
            "model_id": self.model_id,
            "source_repo": self.source_repo,
            "local_path": str(self.local_path),
            "quantization": self.quantization,
            "expected_runtime": self.expected_runtime,
            "required_packages": list(self.required_packages),
            "health_check_files": list(self.health_check_files),
            "present": self.present,
            "disk_size_bytes": size_bytes,
            "cleanup_policy": self.cleanup_policy,
        }


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def default_models(model_root: Path = DEFAULT_MODEL_ROOT) -> list[LocalModel]:
    return [
        LocalModel(
            model_id="mineru2.5-pro-2604-1.2b-mlx-bf16",
            source_repo="carlesonielfa/MinerU2.5-Pro-2604-1.2B-mlx-bf16",
            local_path=model_root / "carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16",
            quantization="bf16",
            expected_runtime="mlx-vlm direct runtime on Apple Silicon",
            required_packages=("mineru-vl-utils[mlx]", "mlx-vlm", "pymupdf", "pillow"),
            health_check_files=("config.json", "model.safetensors", "processor_config.json", "tokenizer.json"),
            cleanup_policy="cache only; never commit; remove manually when no longer used",
        ),
        LocalModel(
            model_id="mineru2.5-pro-2604-1.2b-mlx-8bit",
            source_repo="carlesonielfa/MinerU2.5-Pro-2604-1.2B-mlx-8bit",
            local_path=model_root / "carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-8bit",
            quantization="8bit",
            expected_runtime="mlx-vlm direct runtime on Apple Silicon",
            required_packages=("mineru-vl-utils[mlx]", "mlx-vlm", "pymupdf", "pillow"),
            health_check_files=("config.json", "model.safetensors", "processor_config.json", "tokenizer.json"),
            cleanup_policy="cache only; never commit; remove manually when no longer used",
        ),
    ]


def registry_snapshot(model_root: Path = DEFAULT_MODEL_ROOT) -> list[dict[str, Any]]:
    return [model.to_dict() for model in default_models(model_root)]
