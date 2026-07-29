"""Lazy Docling adapter for the dependency spike.

This module must remain importable when Docling is not installed. It does not
shell out to the Docling CLI and does not fetch external URLs.
"""

from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .quality import CONVERSION_POLICY, measure_gxx_quality

FORMULA_CROP_PADDING_POINTS = 2.5


class DoclingAdapterError(RuntimeError):
    """Controlled adapter error safe to expose as a short CLI message."""


def _import_docling() -> Any:
    try:
        return import_module("docling")
    except ImportError as exc:
        raise DoclingAdapterError("docling package is not installed") from exc


def _load_document_converter() -> type[Any]:
    try:
        module = import_module("docling.document_converter")
    except ImportError as exc:
        raise DoclingAdapterError("docling DocumentConverter is not available") from exc
    return module.DocumentConverter


def _model_cache_exists(repo_id: str) -> bool:
    cache_dir = Path.home() / ".cache" / "docling" / "models" / repo_id.replace("/", "--")
    return cache_dir.is_dir()


def _granite_mlx_formula_available() -> bool:
    if not _model_cache_exists("ibm-granite/granite-docling-258M-mlx"):
        return False
    try:
        import_module("mlx_vlm")
        import_module("mlx.core")
    except ImportError:
        return False
    return True


def _codeformulav2_available() -> bool:
    return _model_cache_exists("docling-project/CodeFormulaV2")


def _formula_model_for_profile(profile: str) -> str | None:
    if profile == "article_quality_formula_codeformulav2":
        return "codeformulav2" if _codeformulav2_available() else None
    if profile in {"article_quality_formula", "ocr_fallback_mac", "ocr_fallback_auto"}:
        if _granite_mlx_formula_available():
            return "granite_docling_mlx"
        if profile == "article_quality_formula" and _codeformulav2_available():
            return "codeformulav2"
    return None


def _code_formula_options_for_model(pipeline_options: Any, model: str) -> Any:
    if model == "granite_docling_mlx":
        vlm_engine_options = import_module("docling.datamodel.vlm_engine_options")
        return pipeline_options.CodeFormulaVlmOptions.from_preset(
            "granite_docling",
            engine_options=vlm_engine_options.MlxVlmEngineOptions(),
        )
    return pipeline_options.CodeFormulaVlmOptions.from_preset("codeformulav2")


def _prepare_formula_with_tight_padding(
    self: Any,
    conv_res: Any,
    element: Any,
) -> Any:
    label = str(getattr(element, "label", "")).casefold()
    if label not in {"formula", "docitemlabel.formula"}:
        base_model = import_module("docling.models.base_model")
        return base_model.BaseItemAndImageEnrichmentModel.prepare_element(
            self,
            conv_res,
            element,
        )
    if not self.is_processable(doc=conv_res.document, element=element):
        return None
    if not getattr(element, "prov", None):
        return None
    element_prov = element.prov[0]
    bbox = element_prov.bbox
    x_scale = FORMULA_CROP_PADDING_POINTS / max(float(bbox.width), 1.0)
    y_scale = FORMULA_CROP_PADDING_POINTS / max(float(bbox.height), 1.0)
    expanded_bbox = bbox.expand_by_scale(x_scale, y_scale)
    page_ix = element_prov.page_no - conv_res.pages[0].page_no
    cropped_image = conv_res.pages[page_ix].get_image(
        scale=self.images_scale,
        cropbox=expanded_bbox,
    )
    if cropped_image is None:
        return None
    base_models = import_module("docling.datamodel.base_models")
    return base_models.ItemAndImageEnrichmentElement(
        item=element,
        image=cropped_image,
    )


def _configure_formula_crop_padding() -> None:
    module = import_module(
        "docling.models.stages.code_formula.code_formula_vlm_model"
    )
    model_class = module.CodeFormulaVlmModel
    model_class.expansion_factor = 0.0
    model_class.tight_crop_padding_points = FORMULA_CROP_PADDING_POINTS
    model_class.prepare_element = _prepare_formula_with_tight_padding


def _make_document_converter(profile: str) -> Any:
    DocumentConverter = _load_document_converter()
    try:
        document_converter = import_module("docling.document_converter")
        accelerator_options = import_module("docling.datamodel.accelerator_options")
        base_models = import_module("docling.datamodel.base_models")
        pipeline_options = import_module("docling.datamodel.pipeline_options")
        artifacts_path = Path.home() / ".cache" / "docling" / "models"
        artifacts_path_value = str(artifacts_path) if artifacts_path.is_dir() else None
        formula_model = _formula_model_for_profile(profile)
        if formula_model is not None:
            _configure_formula_crop_padding()
        option_kwargs: dict[str, Any] = {
            "accelerator_options": accelerator_options.AcceleratorOptions(device="cpu"),
            "do_ocr": False,
            "do_table_structure": False,
            "do_formula_enrichment": False,
            "generate_page_images": False,
            "generate_picture_images": False,
            "generate_table_images": False,
            "images_scale": 3.0,
            "artifacts_path": artifacts_path_value,
        }
        if profile == "text_quality_probe":
            pass
        elif profile == "quality_first":
            option_kwargs.update(
                {
                    "do_table_structure": True,
                    "table_structure_options": pipeline_options.TableStructureOptions(
                        do_cell_matching=True,
                        mode=pipeline_options.TableFormerMode.ACCURATE,
                    ),
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "generate_table_images": True,
                }
            )
        elif profile == "article_quality_formula":
            option_kwargs.update(
                {
                    "do_table_structure": True,
                    "table_structure_options": pipeline_options.TableStructureOptions(
                        do_cell_matching=True,
                        mode=pipeline_options.TableFormerMode.ACCURATE,
                    ),
                    "do_formula_enrichment": formula_model is not None,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "generate_table_images": True,
                }
            )
            if formula_model is not None:
                option_kwargs["code_formula_options"] = _code_formula_options_for_model(
                    pipeline_options,
                    formula_model,
                )
        elif profile == "article_quality_formula_codeformulav2":
            option_kwargs.update(
                {
                    "do_table_structure": True,
                    "table_structure_options": pipeline_options.TableStructureOptions(
                        do_cell_matching=True,
                        mode=pipeline_options.TableFormerMode.ACCURATE,
                    ),
                    "do_formula_enrichment": formula_model is not None,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "generate_table_images": True,
                }
            )
            if formula_model is not None:
                option_kwargs["code_formula_options"] = _code_formula_options_for_model(
                    pipeline_options,
                    formula_model,
                )
        elif profile == "quality_without_table_structure":
            option_kwargs.update(
                {
                    "generate_page_images": True,
                    "generate_picture_images": True,
                }
            )
        elif profile == "ocr_fallback_mac":
            option_kwargs.update(
                {
                    "do_ocr": True,
                    "do_table_structure": True,
                    "table_structure_options": pipeline_options.TableStructureOptions(
                        do_cell_matching=True,
                        mode=pipeline_options.TableFormerMode.ACCURATE,
                    ),
                    "do_formula_enrichment": formula_model is not None,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "generate_table_images": True,
                    "ocr_options": pipeline_options.OcrMacOptions(
                        lang=["zh-Hans", "zh-Hant", "en-US"],
                        force_full_page_ocr=True,
                    ),
                }
            )
            if formula_model is not None:
                option_kwargs["code_formula_options"] = _code_formula_options_for_model(
                    pipeline_options,
                    formula_model,
                )
        elif profile == "ocr_fallback_auto":
            option_kwargs.update(
                {
                    "do_ocr": True,
                    "do_table_structure": True,
                    "table_structure_options": pipeline_options.TableStructureOptions(
                        do_cell_matching=True,
                        mode=pipeline_options.TableFormerMode.ACCURATE,
                    ),
                    "do_formula_enrichment": formula_model is not None,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "generate_table_images": True,
                    "ocr_options": pipeline_options.OcrAutoOptions(
                        lang=["chinese", "english"],
                        force_full_page_ocr=True,
                    ),
                }
            )
            if formula_model is not None:
                option_kwargs["code_formula_options"] = _code_formula_options_for_model(
                    pipeline_options,
                    formula_model,
                )
        options = pipeline_options.PdfPipelineOptions(**option_kwargs)
        return DocumentConverter(
            allowed_formats=[base_models.InputFormat.PDF],
            format_options={
                base_models.InputFormat.PDF: document_converter.PdfFormatOption(
                    pipeline_options=options,
                ),
            },
        )
    except Exception:
        return DocumentConverter()


def is_docling_available() -> bool:
    try:
        _import_docling()
    except DoclingAdapterError:
        return False
    return True


def get_docling_version() -> str | None:
    try:
        return metadata.version("docling")
    except metadata.PackageNotFoundError:
        return None


def _is_remote_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https", "ftp"}


def _optional_export(document: Any, method_names: tuple[str, ...], label: str) -> tuple[Any | None, str | None]:
    for method_name in method_names:
        method = getattr(document, method_name, None)
        if method is None:
            continue
        try:
            return method(), None
        except Exception:
            return None, f"{label}_export_failed"
    return None, None


def _export_document_payload(
    *,
    result: Any,
    docling_version: str | None,
    warnings: list[str],
    profile: str,
) -> dict[str, Any]:
    document = result.document
    try:
        markdown = document.export_to_markdown()
        html = document.export_to_html()
        document_dict = document.export_to_dict()
    except Exception as exc:
        raise DoclingAdapterError(f"docling core export failed: {exc.__class__.__name__}") from exc

    text, warning = _optional_export(document, ("export_to_text",), "text")
    if warning:
        warnings.append(warning)

    doctags, warning = _optional_export(
        document,
        ("export_to_doctags", "export_to_document_tokens"),
        "doctags",
    )
    if warning:
        warnings.append(warning)

    text_quality = measure_gxx_quality(markdown, html, text)
    formula_model = _formula_model_for_profile(profile)
    feature_warnings = list(warnings)
    if profile in {
        "quality_first",
        "article_quality_formula",
        "article_quality_formula_codeformulav2",
        "ocr_fallback_mac",
        "ocr_fallback_auto",
    }:
        feature_warnings.append("table_structure_accurate_cell_matching_enabled")
        if formula_model == "granite_docling_mlx":
            feature_warnings.append("formula_enrichment_enabled_granite_docling_mlx")
        elif formula_model == "codeformulav2":
            feature_warnings.append("formula_enrichment_enabled_codeformula_v2")
        elif profile in {"article_quality_formula", "article_quality_formula_codeformulav2"}:
            feature_warnings.append("formula_enrichment_unavailable_missing_local_model_or_runtime")
        elif profile in {"ocr_fallback_mac", "ocr_fallback_auto"}:
            feature_warnings.append(
                "formula_enrichment_unavailable_for_ocr_fallback; high_res_review_fallback_enabled"
            )
    return {
        "markdown": markdown,
        "html": html,
        "document_dict": document_dict,
        "document": document,
        "text": text,
        "doctags": doctags,
        "warnings": feature_warnings,
        "docling_version": docling_version,
        "result": result,
        "conversion_policy": CONVERSION_POLICY,
        "conversion_profile": profile,
        "ocr_fallback_used": False,
        "text_quality_gxx_count": text_quality.gxx_count,
        "text_quality_gxx_density": text_quality.gxx_density,
        "text_quality_failed": text_quality.failed,
        "formula_enrichment_enabled": formula_model is not None,
        "formula_model": formula_model,
    }


def _run_docling_profile(input_path: Path, profile: str, docling_version: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        converter = _make_document_converter(profile)
        result = converter.convert(input_path)
    except Exception as exc:
        raise DoclingAdapterError(f"docling {profile} conversion failed: {exc.__class__.__name__}") from exc
    return _export_document_payload(
        result=result,
        docling_version=docling_version,
        warnings=warnings,
        profile=profile,
    )


def _convert_quality_first(input_path: Path, docling_version: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        probe = _run_docling_profile(input_path, "text_quality_probe", docling_version)
    except DoclingAdapterError as exc:
        warnings.append(f"text_quality_probe_unavailable: {exc}")
        probe = None

    if probe and probe.get("text_quality_failed"):
        conversion = probe
    else:
        try:
            conversion = _run_docling_profile(input_path, "article_quality_formula", docling_version)
            if warnings:
                conversion["warnings"] = warnings + list(conversion.get("warnings") or [])
        except DoclingAdapterError as exc:
            warnings.append(f"formula_quality_profile_unavailable: {exc}")
            try:
                conversion = _run_docling_profile(
                    input_path,
                    "article_quality_formula_codeformulav2",
                    docling_version,
                )
            except DoclingAdapterError as fallback_exc:
                warnings.append(f"codeformulav2_formula_profile_unavailable: {fallback_exc}")
                try:
                    conversion = _run_docling_profile(input_path, "quality_first", docling_version)
                except DoclingAdapterError as quality_exc:
                    warnings.append(f"table_or_asset_quality_profile_unavailable: {quality_exc}")
                    try:
                        conversion = _run_docling_profile(
                            input_path,
                            "quality_without_table_structure",
                            docling_version,
                        )
                    except DoclingAdapterError as asset_exc:
                        warnings.append(f"asset_generation_profile_unavailable: {asset_exc}")
                        conversion = _run_docling_profile(input_path, "compatibility", docling_version)
            conversion["warnings"] = warnings + list(conversion.get("warnings") or [])

    if conversion.get("text_quality_failed"):
        conversion.setdefault("warnings", []).append(
            "text_quality_failed_gxx_density; attempting OCR fallback"
        )
        if probe and conversion is not probe:
            conversion.setdefault("warnings", []).append(
                "article_quality_profile_preserved_bad_text_signal_from_probe"
            )
        for profile in ("ocr_fallback_mac", "ocr_fallback_auto"):
            try:
                fallback = _run_docling_profile(input_path, profile, docling_version)
            except DoclingAdapterError as exc:
                conversion.setdefault("warnings", []).append(f"{profile}_failed: {exc}")
                continue
            fallback_quality = measure_gxx_quality(
                fallback.get("markdown"),
                fallback.get("html"),
                fallback.get("text"),
            )
            current_quality = measure_gxx_quality(
                conversion.get("markdown"),
                conversion.get("html"),
                conversion.get("text"),
            )
            if (
                not fallback_quality.failed
                or fallback_quality.gxx_count < current_quality.gxx_count
            ):
                fallback["ocr_fallback_used"] = True
                fallback["warnings"] = list(conversion.get("warnings") or []) + [
                    f"{profile}_used_after_gxx_quality_failure"
                ] + list(fallback.get("warnings") or [])
                return fallback
            conversion.setdefault("warnings", []).append(
                f"{profile}_did_not_improve_gxx_quality"
            )

    return conversion


def convert_with_docling(
    *,
    job_uuid: str,
    input_file_path: str | Path,
    output_root: str | Path | None = None,
    display_name: str | None = None,
    original_name: str | None = None,
    source_name: str | None = None,
    image_export_mode: str = "referenced",
    requested_outputs: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run Docling's Python API and return exported document payloads."""
    _ = (
        job_uuid,
        output_root,
        display_name,
        original_name,
        source_name,
        image_export_mode,
        requested_outputs,
        started_at,
        finished_at,
        duration_seconds,
    )
    input_value = str(input_file_path)
    if _is_remote_url(input_value):
        raise DoclingAdapterError("input_file_path must be a local file path")

    input_path = Path(input_file_path).expanduser()
    if not input_path.exists() or not input_path.is_file():
        raise DoclingAdapterError("input_file_path must exist and be a local file")

    docling_version = get_docling_version()

    return _convert_quality_first(input_path, docling_version)
