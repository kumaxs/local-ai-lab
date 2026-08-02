from __future__ import annotations

import base64
import importlib.util
import threading
import unittest

from docling_service.formula_api import (
    _ambiguity_reasons,
    _needs_fallback,
    _normalize_model_latex,
    _safety_reasons,
    _semantic_coverage,
    _strip_equation_number,
    FormulaRuntime,
    create_formula_app,
)


class FakeRuntime:
    model_repo = "test/unimernet-small"
    fallback_model_name = "test/pp-formulanet-l"
    engine_name = "test/guarded-ensemble"
    device = "cpu"
    model = None
    prepared = False

    def load(self) -> None:
        self.model = object()
        self.prepared = True

    def recognize(self, items):
        return [
            {
                "id": item["id"],
                "latex": "x = y",
                "ok": True,
                "safety_reasons": [],
                "variant": "source_crop",
            }
            for item in items
        ]


class FormulaApiTests(unittest.TestCase):
    def test_load_prepares_models_without_leaving_primary_resident(self) -> None:
        runtime = FormulaRuntime.__new__(FormulaRuntime)
        runtime.prepared = False
        calls: list[str] = []
        runtime._prepare_primary = lambda: calls.append("primary")
        runtime._prepare_fallback = lambda: calls.append("fallback")
        runtime._load_primary = lambda: calls.append("resident")

        runtime.load()

        self.assertTrue(runtime.prepared)
        self.assertEqual(calls, ["primary", "fallback"])

    def test_equation_number_is_removed_before_adapter_reanchors_it(self) -> None:
        self.assertEqual(
            _strip_equation_number(r"x = y \quad ( 12 )", 12),
            "x = y",
        )

    def test_repeated_equation_spacing_is_removed_with_number(self) -> None:
        self.assertEqual(
            _strip_equation_number(r"x = y \qquad \qquad ( 12 )", 12),
            "x = y",
        )

    def test_model_specific_commands_are_normalized(self) -> None:
        normalized, repairs = _normalize_model_latex(
            r"\operatorname* { m i n }_x \pmb{x} + \varDelta x"
        )
        self.assertEqual(normalized, r"\min_x \boldsymbol{x} + \Delta x")
        self.assertIn("normalized_spaced_min_operator", repairs)

    def test_spaced_operators_and_nested_subscripts_are_normalized(self) -> None:
        normalized, repairs = _normalize_model_latex(
            r"e t _ { q _ { _ { i } \rightarrow c _ { p } } } "
            r"= \mathrm { s o f t m a x }(x)"
        )

        self.assertEqual(
            normalized,
            r"e t _ { q _{i \rightarrow c _ { p }} } = \operatorname{softmax}(x)",
        )
        self.assertIn("flattened_malformed_nested_subscript", repairs)
        self.assertIn("normalized_spaced_softmax_operator", repairs)

    def test_spaced_formula_words_are_compacted_without_joining_products(self) -> None:
        normalized, repairs = _normalize_model_latex(
            r"\begin{array}{r l r l}x_k^{r a b b i t}"
            r"+\mathrm{s u b j e c t \, t o}"
            r"+\mathbf{s u b j e c t \ t o}+\mathbf{M A E}"
            r"+a b c\end{array}"
        )

        self.assertEqual(
            normalized,
            r"\begin{array}{r l r l}x_k^{\mathrm{rabbit}}"
            r"+\mathrm{subject \, to}"
            r"+\mathbf{subject \, to}+\mathbf{M A E}"
            r"+a b c\end{array}",
        )
        self.assertIn("compacted_spaced_formula_words", repairs)

    def test_plain_prose_to_does_not_masquerade_as_arrow_semantics(self) -> None:
        coverage = _semantic_coverage(
            "Vcell = E - Vact, due to electrode kinetics",
            r"V_{cell}=E-V_{act}",
        )

        self.assertEqual(coverage["score"], 1.0)
        self.assertNotIn("to", coverage["source_tokens"])

    def test_missing_pdf_semantics_trigger_guarded_fallback(self) -> None:
        coverage = _semantic_coverage(
            "γ∗ = min sup γ, α,ε G∈{G}[λ,λ]",
            r"\gamma^*=\min_{\alpha,\varepsilon}\sup\gamma",
        )
        self.assertLess(coverage["score"], 0.82)
        self.assertIn("lambda", coverage["missing_tokens"])
        self.assertTrue(_needs_fallback([], coverage, 0.82))

    def test_complete_fallback_formula_meets_pdf_semantic_gate(self) -> None:
        coverage = _semantic_coverage(
            "γ∗ = min sup γ, α,ε G∈{G}[λ,λ]",
            r"\gamma^*=\min_{\alpha,\varepsilon}"
            r"\underset{\mathcal{G}\in\{\mathcal{G}\}_{[\lambda,\lambda]}}{\sup}\gamma",
        )
        self.assertEqual(coverage["score"], 1.0)
        self.assertFalse(_needs_fallback([], coverage, 0.82))

    def test_repeated_generation_is_rejected(self) -> None:
        self.assertIn(
            "repeated_spacing",
            _safety_reasons("x" + r" \qquad" * 12),
        )
        self.assertIn(
            "repeated_operator_hallucination",
            _safety_reasons("x" + r" \times" * 16),
        )

    def test_visible_left_brace_does_not_unbalance_latex_groups(self) -> None:
        formula = (
            r"\left\{ \begin{array}{l} x_i = 1 \\ y_i = 2 "
            r"\end{array} \right."
        )

        self.assertNotIn("unbalanced_braces", _safety_reasons(formula))
        self.assertNotIn("left_right_mismatch", _safety_reasons(formula))

    def test_unbalanced_left_right_delimiters_are_rejected(self) -> None:
        self.assertIn(
            "left_right_mismatch",
            _safety_reasons(r"\left( x + y"),
        )

    def test_malformed_nested_subscript_is_rejected(self) -> None:
        self.assertIn(
            "malformed_nested_subscript",
            _safety_reasons(r"x_{_{i}} = y"),
        )

    def test_repeated_ellipsis_hallucination_is_rejected(self) -> None:
        self.assertIn(
            "repeated_ellipsis_hallucination",
            _safety_reasons("x=y" + r" \cdots" * 10),
        )

    def test_indexed_direct_sum_requires_guarded_cross_check(self) -> None:
        self.assertEqual(
            _ambiguity_reasons(r"x \bigoplus_{i=1}^{n} y_i"),
            ["indexed_direct_sum_requires_cross_check"],
        )
        self.assertEqual(_ambiguity_reasons(r"x \oplus \sum_{i=1}^{n}y_i"), [])

    def test_private_batch_endpoint(self) -> None:
        if not all(
            importlib.util.find_spec(name) is not None
            for name in ("fastapi", "pydantic", "httpx")
        ):
            self.skipTest("HTTP formula service dependencies are not installed")
        from fastapi.testclient import TestClient

        runtime = FakeRuntime()
        app = create_formula_app(runtime)
        with TestClient(app) as client:
            health = client.get("/healthz")
            response = client.post(
                "/v1/recognize",
                json={
                    "items": [
                        {
                            "id": "1",
                            "image_base64": base64.b64encode(b"image").decode("ascii"),
                            "equation_number": 1,
                        }
                    ]
                },
            )

        self.assertTrue(health.json()["ok"])
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["results"][0]["latex"], "x = y")

    def test_force_fallback_routes_post_validation_retry_to_pp_model(self) -> None:
        runtime = FormulaRuntime.__new__(FormulaRuntime)
        runtime._lock = threading.Lock()
        runtime.minimum_coverage = 0.82
        runtime._recognize_primary = lambda _images: ["x = y"]
        runtime._recognize_fallback = lambda _images: ["x = z"]
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
            "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
        )

        result = runtime.recognize(
            [
                {
                    "id": "retry",
                    "image_base64": png,
                    "fallback_image_base64": png,
                    "source_text": None,
                    "equation_number": None,
                    "force_fallback": True,
                }
            ]
        )[0]

        self.assertTrue(result["ok"])
        self.assertEqual(result["latex"], "x = z")
        self.assertEqual(result["variant"], "pp_formulanet_l_guarded_fallback")

    def test_bold_sigma_at_start_of_summand_gets_visual_cross_check(self) -> None:
        runtime = FormulaRuntime.__new__(FormulaRuntime)
        runtime._lock = threading.Lock()
        runtime.minimum_coverage = 0.82
        runtime._recognize_primary = lambda _images: [
            r"L=-\sum_{t}\left({\bf{\sigma}}_{t}\ln y_t\right)"
        ]
        runtime._recognize_fallback = lambda _images: [
            r"L=-\sum_{t}\left(r_t\ln y_t\right)"
        ]
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
            "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
        )

        result = runtime.recognize(
            [{"id": "sigma", "image_base64": png, "source_text": None}]
        )[0]

        self.assertTrue(result["ok"])
        self.assertEqual(result["latex"], r"L=-\sum_{t}\left(r_t\ln y_t\right)")
        self.assertEqual(result["variant"], "pp_formulanet_l_guarded_fallback")
        self.assertIn(
            "bold_sigma_summand_requires_cross_check",
            result["primary"]["ambiguity_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
