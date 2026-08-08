"""Private, quality-gated formula recognition for the Docker release.

Linux Docling performs layout detection with formula enrichment disabled.  The
resulting crops are sent to this private service.  UniMERNet-Small is the
quality-first primary recognizer.  PP-FormulaNet-L is loaded only when the
primary result fails a structural check or omits semantic tokens visible in the
original PDF text layer.  The two models are never retained in memory at the
same time.
"""

from __future__ import annotations

import base64
import ctypes
import gc
import io
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from pydantic import BaseModel, Field
except ModuleNotFoundError:  # Keep pure helper tests importable in minimal Python.
    class BaseModel:  # type: ignore[no-redef]
        pass

    def Field(default: Any = None, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return default


FORMULA_SERVICE_VERSION = "1.1.0"
DEFAULT_MODEL_REPO = "wanderkid/unimernet_small"
DEFAULT_FALLBACK_MODEL = "PP-FormulaNet-L"


class FormulaItem(BaseModel):
    id: str
    image_base64: str = Field(min_length=1)
    fallback_image_base64: str | None = None
    source_text: str | None = Field(default=None, max_length=12000)
    equation_number: int | None = None
    force_fallback: bool = False


class FormulaBatch(BaseModel):
    items: list[FormulaItem] = Field(min_length=1, max_length=256)


def _checkpoint_path(model_dir: Path) -> Path:
    checkpoints = sorted(model_dir.glob("*.pth"))
    if len(checkpoints) != 1:
        raise RuntimeError(
            f"expected one UniMERNet checkpoint in {model_dir}, found {len(checkpoints)}"
        )
    return checkpoints[0]


def _strip_equation_number(latex: str, equation_number: int | None) -> str:
    text = latex.strip().removeprefix("$$").removesuffix("$$").strip()
    text = re.sub(r"</?formula\b[^>]*>", "", text, flags=re.IGNORECASE)
    if equation_number is not None:
        number = re.escape(str(equation_number))
        patterns = (
            rf"\s*\\eqno\s*\(?\s*{number}\s*\)?\s*$",
            rf"\s*(?:\\(?:qquad|quad)\s*)+\(?\s*{number}\s*\)?\s*$",
            rf"\s*\\?left\s*\(\s*{number}\s*\\?right\s*\)\s*$",
            rf"\s*\(?\s*{number}\s*\)?\s*$",
        )
        for pattern in patterns:
            updated = re.sub(pattern, "", text)
            if updated != text:
                text = updated.strip()
                break
    return text


_NAMED_OPERATOR_REPLACEMENTS = {
    "min": r"\min",
    "max": r"\max",
    "sup": r"\sup",
    "inf": r"\inf",
    "lim": r"\lim",
    "log": r"\log",
    "exp": r"\exp",
    "sin": r"\sin",
    "cos": r"\cos",
    "tan": r"\tan",
    "tanh": r"\tanh",
    "softmax": r"\operatorname{softmax}",
    "relu": r"\operatorname{ReLU}",
    "sigmoid": r"\operatorname{sigmoid}",
    "mlp": r"\operatorname{MLP}",
}

_PLAIN_SEMANTIC_WORDS = {
    "min",
    "max",
    "sup",
    "inf",
    "lim",
    "log",
    "exp",
    "sin",
    "cos",
    "tan",
}


def _compact_spaced_formula_words(text: str) -> tuple[str, int]:
    """Compact OCR-spaced words without joining ordinary variable products."""

    count = 0
    spaced_word = re.compile(
        r"(?<![A-Za-z])(?:[A-Za-z]\s+){1,}[A-Za-z](?![A-Za-z])"
    )

    def compact_wrapper(match: re.Match[str]) -> str:
        nonlocal count
        command = match.group("command")
        content = re.sub(r"\\\s+", r"\\, ", match.group("content"))

        def compact_word(word_match: re.Match[str]) -> str:
            nonlocal count
            letters = re.sub(r"\s+", "", word_match.group(0))
            if command in {"mathbf", "boldsymbol"} and not letters.islower():
                return word_match.group(0)
            count += 1
            return letters

        updated = spaced_word.sub(compact_word, content)
        return rf"\{command}{{{updated.strip()}}}"

    text = re.sub(
        r"\\(?P<command>mathrm|text|operatorname\*?|mathbf|boldsymbol)\s*\{"
        r"(?P<content>[^{}]*)\}",
        compact_wrapper,
        text,
    )

    def compact_bare_group(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        compact = re.sub(r"\s+", "", match.group("word"))
        return match.group("script") + r"{\mathrm{" + compact + "}}"

    # Four or more lowercase letters are overwhelmingly an OCR-spaced label
    # (rabbit, cell, test, conc) rather than a product of scalar variables.
    text = re.sub(
        r"(?P<script>[_^])\s*\{\s*"
        r"(?P<word>(?:[a-z]\s+){3,}[a-z])\s*\}",
        compact_bare_group,
        text,
    )
    return text, count


def _matching_group_brace(text: str, opening: int) -> int | None:
    depth = 0
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _flatten_nested_subscripts(text: str) -> tuple[str, int]:
    """Flatten the invalid but unambiguous OCR shape ``x_{_{i} tail}``."""

    count = 0
    search_from = 0
    pattern = re.compile(r"_\s*\{\s*_\s*\{")
    while match := pattern.search(text, search_from):
        outer_open = text.find("{", match.start())
        inner_open = match.end() - 1
        inner_close = _matching_group_brace(text, inner_open)
        outer_close = _matching_group_brace(text, outer_open)
        if (
            inner_close is None
            or outer_close is None
            or inner_close >= outer_close
        ):
            search_from = match.end()
            continue
        inner = text[inner_open + 1 : inner_close].strip()
        tail = text[inner_close + 1 : outer_close].strip()
        replacement = "_{" + inner + ((" " + tail) if tail else "") + "}"
        text = text[: match.start()] + replacement + text[outer_close + 1 :]
        search_from = match.start() + len(replacement)
        count += 1
    return text, count


def _normalize_model_latex(latex: str) -> tuple[str, list[str]]:
    """Normalize model dialects without inventing mathematical content."""

    text = latex.strip()
    repairs: list[str] = []
    for name, replacement in _NAMED_OPERATOR_REPLACEMENTS.items():
        spaced = r"\s*".join(re.escape(char) for char in name)
        pattern = re.compile(
            rf"\\(?:operatorname\*?|mathrm|text)\s*\{{\s*{spaced}\s*\}}",
            flags=re.IGNORECASE,
        )
        updated = pattern.sub(lambda _match, value=replacement: value, text)
        if updated != text:
            text = updated
            repairs.append(f"normalized_spaced_{name}_operator")
    text, compacted_words = _compact_spaced_formula_words(text)
    if compacted_words:
        repairs.append("compacted_spaced_formula_words")
    updated = re.sub(r"\\varDelta\b", r"\\Delta", text)
    if updated != text:
        text = updated
        repairs.append("normalized_varDelta_to_Delta")
    updated = re.sub(r"\\pmb\b", r"\\boldsymbol", text)
    if updated != text:
        text = updated
        repairs.append("normalized_pmb_to_boldsymbol")
    text, flattened_subscripts = _flatten_nested_subscripts(text)
    if flattened_subscripts:
        repairs.append("flattened_malformed_nested_subscript")
    return re.sub(r"[ \t]+", " ", text).strip(), list(dict.fromkeys(repairs))


def _safety_reasons(latex: str) -> list[str]:
    reasons: list[str] = []
    if not latex.strip():
        reasons.append("empty")
    if len(latex) > 6000:
        reasons.append("excessive_length")
    if len(re.findall(r"\\qquad\b", latex)) > 8:
        reasons.append("repeated_spacing")
    if re.search(r"(?:\\quad\s*){20,}", latex):
        reasons.append("repeated_spacing")
    if len(re.findall(r"\\!", latex)) > 24:
        reasons.append("repeated_spacing")
    if len(re.findall(r"\\(?:times|cdot)\b", latex)) > 12:
        reasons.append("repeated_operator_hallucination")
    if re.search(r"(?:\bO\s*){20,}", latex):
        reasons.append("repeated_glyph")
    if re.search(r"</?formula\b", latex, flags=re.IGNORECASE):
        reasons.append("raw_model_token")
    brace_depth = 0
    escaped = False
    for char in latex:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
            if brace_depth < 0:
                break
    if brace_depth != 0:
        reasons.append("unbalanced_braces")
    if latex.count(r"\begin{") != latex.count(r"\end{"):
        reasons.append("unbalanced_environment")
    left_count = len(re.findall(r"\\left(?=\s|[\(\[\{\\])", latex))
    # ``\right.`` is a valid invisible delimiter for cases/array layouts.
    right_count = len(re.findall(r"\\right(?=\s|[\)\]\}\\.])", latex))
    if left_count != right_count:
        reasons.append("left_right_mismatch")
    if re.search(r"_\s*\{\s*(?:\\[,;:!]|\s)*_\s*\{", latex):
        reasons.append("malformed_nested_subscript")
    if len(re.findall(r"\\(?:c?dots|ldots)\b", latex)) > 6:
        reasons.append("repeated_ellipsis_hallucination")
    atoms = re.findall(
        r"\\(?:mathfrak|mathrm|mathbf|mathcal|text)\s*\{\s*[^{}]{1,16}\s*\}",
        latex,
    )
    if atoms:
        most_common = max(atoms.count(atom) for atom in set(atoms))
        if most_common >= 8 and most_common / len(atoms) >= 0.45:
            reasons.append("repeated_tex_atom_hallucination")
    if re.search(r"\\(?:varDelta|pmb)\b", latex):
        reasons.append("unsupported_model_command")
    return list(dict.fromkeys(reasons))


_SEMANTIC_COMMANDS = {
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "varepsilon",
    "theta",
    "lambda",
    "mu",
    "nu",
    "greekxi",
    "pi",
    "rho",
    "sigma",
    "tau",
    "phi",
    "varphi",
    "psi",
    "omega",
    "min",
    "max",
    "sup",
    "inf",
    "lim",
    "sum",
    "prod",
    "int",
    "log",
    "exp",
    "sin",
    "cos",
    "tan",
    "forall",
    "exists",
    "in",
    "notin",
    "to",
    "otimes",
    "le",
    "ge",
}

_UNICODE_SEMANTICS = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "∆": "delta",
    "Δ": "delta",
    "ε": "epsilon",
    "θ": "theta",
    "λ": "lambda",
    "µ": "mu",
    "μ": "mu",
    "ν": "nu",
    "ξ": "greekxi",
    "π": "pi",
    "ρ": "rho",
    "σ": "sigma",
    "τ": "tau",
    "φ": "phi",
    "ψ": "psi",
    "ω": "omega",
    "∑": "sum",
    "∏": "prod",
    "∫": "int",
    "∀": "forall",
    "∃": "exists",
    "∈": "in",
    "∉": "notin",
    "⊗": "otimes",
    "→": "to",
    "≤": "le",
    "⩽": "le",
    "≥": "ge",
    "⩾": "ge",
}


def _semantic_tokens(text: str) -> list[str]:
    """Extract conservative symbols that are comparable across PDF and TeX."""

    normalized = text
    explicit_tokens: list[str] = []
    for source, replacement in _UNICODE_SEMANTICS.items():
        explicit_tokens.extend([replacement] * normalized.count(source))
        normalized = normalized.replace(source, " ")
    normalized = re.sub(
        r"\\(?:mathcal|mathbb|mathbf|boldsymbol|mathrm)\s*\{\s*([A-Za-z])\s*\}",
        r" \1 ",
        normalized,
    )

    def record_command(match: re.Match[str]) -> str:
        folded = match.group(1).casefold()
        if folded == "xi":
            folded = "greekxi"
        if folded in _SEMANTIC_COMMANDS:
            explicit_tokens.append("epsilon" if folded == "varepsilon" else folded)
        return " "

    normalized = re.sub(
        r"\\([A-Za-z]+)",
        record_command,
        normalized,
    )
    for name in _NAMED_OPERATOR_REPLACEMENTS:
        spaced = r"\b" + r"\s*".join(name) + r"\b"
        normalized = re.sub(spaced, f" {name} ", normalized, flags=re.IGNORECASE)
    words = re.findall(r"[A-Za-z]+", normalized)
    tokens: list[str] = list(explicit_tokens)
    for word in words:
        folded = word.casefold()
        if folded == "vardelta":
            folded = "delta"
        if folded in _PLAIN_SEMANTIC_WORDS:
            tokens.append("epsilon" if folded == "varepsilon" else folded)
        elif len(word) == 1 and word.isupper():
            tokens.append(folded)
    return tokens


def _semantic_coverage(source_text: str | None, latex: str) -> dict[str, Any]:
    source_tokens = _semantic_tokens(source_text or "")
    candidate_tokens = _semantic_tokens(latex)
    if not source_tokens:
        return {
            "available": False,
            "score": None,
            "source_tokens": [],
            "candidate_tokens": candidate_tokens,
            "missing_tokens": [],
        }
    source_unique = list(dict.fromkeys(source_tokens))
    candidate_unique = list(dict.fromkeys(candidate_tokens))
    candidate_set = set(candidate_unique)
    matched = sum(token in candidate_set for token in source_unique)
    missing = [token for token in source_unique if token not in candidate_set]
    return {
        "available": True,
        "score": round(matched / len(source_unique), 4),
        "source_tokens": source_unique,
        "candidate_tokens": candidate_unique,
        "missing_tokens": missing,
    }


def _needs_fallback(
    reasons: list[str],
    coverage: dict[str, Any],
    minimum_coverage: float,
) -> bool:
    if reasons:
        return True
    score = coverage.get("score")
    return bool(
        coverage.get("available")
        and len(coverage.get("source_tokens") or []) >= 3
        and isinstance(score, (int, float))
        and score < minimum_coverage
    )


def _ambiguity_reasons(latex: str) -> list[str]:
    """Return valid TeX patterns that require a second visual opinion."""

    reasons: list[str] = []
    if re.search(
        r"\\(?:bigoplus|oplus)\s*_\s*\{[^{}]{1,80}\}"
        r"\s*\^\s*\{[^{}]{1,80}\}",
        latex,
    ):
        reasons.append("indexed_direct_sum_requires_cross_check")
    # UniMERNet occasionally turns a small italic ``r`` at the start of a
    # summand into a bold sigma.  It is valid TeX, so structural validation
    # alone cannot catch it.  Ask the independent fallback model for a visual
    # cross-check instead of rewriting the symbol heuristically.
    if re.search(
        r"\\sum\s*_\s*(?:\{[^{}]{1,80}\}|[A-Za-z0-9])"
        r"[\s\\,;:!]*(?:\\left\s*[({[]\s*)?"
        r"(?:\{\s*){1,4}\\(?:bf|mathbf)\s*\{\s*\\sigma\s*\}",
        latex,
    ):
        reasons.append("bold_sigma_summand_requires_cross_check")
    return reasons


def _decode_image(value: str) -> Any:
    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB")


def _release_process_memory() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class FormulaRuntime:
    def __init__(self) -> None:
        self.model_repo = os.getenv("DOCLING_FORMULA_MODEL", DEFAULT_MODEL_REPO)
        self.fallback_model_name = os.getenv(
            "DOCLING_FORMULA_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL
        )
        self.model_root = Path(os.getenv("DOCLING_FORMULA_MODEL_ROOT", "/models/unimernet"))
        self.model_dir = self.model_root / self.model_repo.rsplit("/", 1)[-1]
        self.device = "cpu"
        self.batch_size = max(1, int(os.getenv("DOCLING_FORMULA_BATCH_SIZE", "1")))
        self.minimum_coverage = min(
            1.0,
            max(0.0, float(os.getenv("DOCLING_FORMULA_MIN_SOURCE_COVERAGE", "0.82"))),
        )
        self.model: Any | None = None
        self.processor: Any | None = None
        self.prepared = False
        self._lock = threading.Lock()

    @property
    def engine_name(self) -> str:
        return f"{self.model_repo}+{self.fallback_model_name}"

    def _prepare_primary(self) -> None:
        from huggingface_hub import snapshot_download

        self.model_root.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=self.model_repo, local_dir=self.model_dir)

    def _load_primary(self) -> None:
        if self.model is not None and self.processor is not None:
            return
        import torch
        from omegaconf import OmegaConf
        from unimernet.models.unimernet.unimernet import UniMERModel
        from unimernet.processors import load_processor

        config = OmegaConf.create(
            {
                "model_name": self.model_dir.name,
                "tokenizer_name": "nougat",
                "tokenizer_config": {"path": str(self.model_dir)},
                "model_config": {
                    "model_name": str(self.model_dir),
                    "max_seq_len": 1536,
                },
                "load_finetuned": False,
                "load_pretrained": True,
                "pretrained": str(_checkpoint_path(self.model_dir)),
            }
        )
        self.model = UniMERModel.from_config(config).to(self.device).eval()
        self.processor = load_processor(
            "formula_image_eval",
            OmegaConf.create({"image_size": [192, 672]}),
        )
        torch.set_grad_enabled(False)

    def _unload_primary(self) -> None:
        self.model = None
        self.processor = None
        _release_process_memory()

    def _load_fallback(self) -> Any:
        from paddleocr import FormulaRecognition

        return FormulaRecognition(
            model_name=self.fallback_model_name,
            device="cpu",
            enable_mkldnn=False,
            cpu_threads=1,
        )

    def _prepare_fallback(self) -> None:
        fallback = self._load_fallback()
        del fallback
        _release_process_memory()

    def load(self) -> None:
        self._prepare_primary()
        self._prepare_fallback()
        self.prepared = True

    def _recognize_primary(self, images: list[Any]) -> list[str]:
        import torch

        self._load_primary()
        if self.model is None or self.processor is None:
            raise RuntimeError("primary formula model is not loaded")
        try:
            results: list[str] = []
            with torch.inference_mode():
                for start in range(0, len(images), self.batch_size):
                    batch = torch.stack(
                        [
                            self.processor(image.convert("RGB"))
                            for image in images[start : start + self.batch_size]
                        ]
                    )
                    output = self.model.generate({"image": batch.to(self.device)})
                    results.extend(str(value) for value in output["pred_str"])
            return results
        finally:
            # Docker runs the parser/OCR and formula engines in separate
            # containers under one shared memory budget.  Keeping UniMERNet
            # resident while the next paper enters OCR can terminate the
            # parser on smaller Docker Desktop allocations.  Model files stay
            # cached in the named volume; only process memory is released.
            self._unload_primary()

    def _recognize_fallback(self, images: list[Any]) -> list[str]:
        import numpy as np

        self._unload_primary()
        fallback = self._load_fallback()
        try:
            arrays = [np.asarray(image.convert("RGB"))[:, :, ::-1] for image in images]
            outputs = list(fallback.predict(input=arrays, batch_size=1))
            results: list[str] = []
            for output in outputs:
                payload = getattr(output, "json", None)
                value = payload.get("res", {}).get("rec_formula") if isinstance(payload, dict) else None
                if not value:
                    try:
                        value = output["rec_formula"]
                    except Exception:
                        value = None
                results.append(str(value or ""))
            return results
        finally:
            del fallback
            _release_process_memory()

    def recognize(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            images = [_decode_image(str(item["image_base64"])) for item in items]
            primary_outputs = self._recognize_primary(images)
            results: list[dict[str, Any]] = []
            fallback_indexes: list[int] = []
            for index, (item, raw) in enumerate(zip(items, primary_outputs)):
                equation_number = item.get("equation_number")
                if not isinstance(equation_number, int):
                    equation_number = None
                stripped = _strip_equation_number(raw, equation_number)
                latex, repairs = _normalize_model_latex(stripped)
                reasons = _safety_reasons(latex)
                ambiguity_reasons = _ambiguity_reasons(latex)
                coverage = _semantic_coverage(item.get("source_text"), latex)
                needs_fallback = _needs_fallback(
                    [*reasons, *ambiguity_reasons],
                    coverage,
                    self.minimum_coverage,
                )
                needs_fallback = needs_fallback or bool(item.get("force_fallback"))
                if needs_fallback:
                    fallback_indexes.append(index)
                results.append(
                    {
                        "id": str(item.get("id") or index + 1),
                        "latex": latex,
                        "ok": not reasons and not needs_fallback,
                        "safety_reasons": reasons,
                        "ambiguity_reasons": ambiguity_reasons,
                        "variant": "unimernet_small_primary",
                        "repairs": repairs,
                        "semantic_coverage": coverage,
                    }
                )

            if fallback_indexes:
                fallback_images = [
                    _decode_image(
                        str(
                            items[index].get("fallback_image_base64")
                            or items[index]["image_base64"]
                        )
                    )
                    for index in fallback_indexes
                ]
                fallback_outputs = self._recognize_fallback(fallback_images)
                for index, raw in zip(fallback_indexes, fallback_outputs):
                    item = items[index]
                    equation_number = item.get("equation_number")
                    if not isinstance(equation_number, int):
                        equation_number = None
                    stripped = _strip_equation_number(raw, equation_number)
                    latex, repairs = _normalize_model_latex(stripped)
                    reasons = _safety_reasons(latex)
                    coverage = _semantic_coverage(item.get("source_text"), latex)
                    still_incomplete = _needs_fallback(
                        reasons, coverage, self.minimum_coverage
                    )
                    if still_incomplete:
                        if not reasons:
                            reasons = ["insufficient_source_semantic_coverage"]
                        results[index]["fallback"] = {
                            "latex": latex,
                            "safety_reasons": reasons,
                            "repairs": repairs,
                            "semantic_coverage": coverage,
                        }
                        results[index]["safety_reasons"] = list(
                            dict.fromkeys(
                                [
                                    *results[index]["safety_reasons"],
                                    "guarded_fallback_rejected",
                                ]
                            )
                        )
                        results[index]["ok"] = False
                        continue
                    results[index] = {
                        "id": results[index]["id"],
                        "latex": latex,
                        "ok": True,
                        "safety_reasons": [],
                        "variant": "pp_formulanet_l_guarded_fallback",
                        "repairs": repairs,
                        "semantic_coverage": coverage,
                        "primary": {
                            "latex": results[index]["latex"],
                            "safety_reasons": results[index]["safety_reasons"],
                            "semantic_coverage": results[index]["semantic_coverage"],
                            "ambiguity_reasons": results[index][
                                "ambiguity_reasons"
                            ],
                        },
                    }
            return results


def create_formula_app(runtime: FormulaRuntime | None = None) -> Any:
    from fastapi import FastAPI, HTTPException

    actual_runtime = runtime or FormulaRuntime()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        actual_runtime.load()
        yield

    app = FastAPI(
        title="Local AI Lab Formula Service",
        version=FORMULA_SERVICE_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": bool(getattr(actual_runtime, "prepared", actual_runtime.model is not None)),
            "service": "docling-formula-service",
            "version": FORMULA_SERVICE_VERSION,
            "model": getattr(actual_runtime, "engine_name", actual_runtime.model_repo),
            "primary_model": actual_runtime.model_repo,
            "fallback_model": getattr(actual_runtime, "fallback_model_name", None),
            "device": actual_runtime.device,
        }

    @app.post("/v1/recognize")
    def recognize(payload: FormulaBatch) -> dict[str, Any]:
        try:
            results = actual_runtime.recognize(
                [item.model_dump() for item in payload.items]
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"formula recognition failed: {exc}") from exc
        return {
            "ok": all(bool(item["ok"]) for item in results),
            "model": getattr(actual_runtime, "engine_name", actual_runtime.model_repo),
            "results": results,
        }

    return app


try:
    app = create_formula_app()
except ModuleNotFoundError as exc:  # Minimal development Python has no web stack.
    if exc.name not in {"fastapi", "pydantic"}:
        raise
    app = None
