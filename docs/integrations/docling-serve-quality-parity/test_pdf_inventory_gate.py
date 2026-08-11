from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from argparse import Namespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402


class _FakeSnapshot:
    def __init__(self, *, size: int, sha256: str) -> None:
        self.path = Path("/tmp/fake-source.pdf")
        self.size = size
        self.sha256 = sha256
        self.verify_calls = 0

    def verify(self) -> None:
        self.verify_calls += 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PdfInventoryGateTests(unittest.TestCase):
    def _inventory(self, *, source_pdf_sha256: str = "a" * 64, page_count: int = 1, reason: str | None = None) -> dict[str, Any]:
        return {
            "available": True,
            "reason": reason,
            "source_pdf_sha256": source_pdf_sha256,
            "page_count": page_count,
            "text_health": {
                "available": True,
                "status": adapter.KIND_HEALTHY,
                "reason": None,
                "page_count": page_count,
                "page_no_continuous": True,
                "pages": [
                    {
                        "page_no": 1,
                        "healthy": True,
                        "reasons": [],
                    }
                ],
            },
            "counts": {
                kind: {"high_confidence": 0, "ambiguous": 0, "records": []}
                for kind in adapter.KIND_ORDER
            },
            "no_structure_proof": {kind: adapter.KIND_HEALTHY for kind in adapter.KIND_ORDER},
        }

    def test_run_pdf_inventory_gate_uses_persisted_source_pdf_and_calls_verify_pre_post(self) -> None:
        data = b"pdf"
        digest = _sha256(data)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(data)
            snapshot = _FakeSnapshot(size=len(data), sha256=digest)
            expected_identity = (source_pdf.stat().st_dev, source_pdf.stat().st_ino)
            observed_paths: list[Path] = []
            original_verify = adapter._verify_job_source

            def spy_verify(
                source_snapshot: object,
                observed_path: Path,
                *,
                expected_identity: tuple[int, int] | None = None,
            ) -> tuple[int, int]:
                observed_paths.append(observed_path)
                return original_verify(
                    source_snapshot,
                    observed_path,
                    expected_identity=expected_identity,
                )

            with (
                patch.object(adapter, "_verify_job_source", side_effect=spy_verify) as verify_spy,
                patch.object(
                    adapter,
                    "pdf_structure_inventory",
                    return_value={"available": True, "reason": None},
                ) as structure_spy,
            ):
                result = adapter._run_pdf_inventory_gate(
                    source_pdf,
                    snapshot,
                    expected_identity=expected_identity,
                )

            self.assertEqual(result["available"], True)
            self.assertEqual(verify_spy.call_count, 2)
            self.assertEqual(snapshot.verify_calls, 2)
            self.assertEqual(
                observed_paths,
                [source_pdf, source_pdf],
            )
            structure_spy.assert_called_once_with(source_pdf)

    def test_run_pdf_inventory_gate_missing_source_pdf_skips_structure_analysis(self) -> None:
        snapshot = _FakeSnapshot(size=0, sha256="0" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            source_pdf = Path(tmp) / "source.pdf"
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("must_not_run"),
            ) as structure_spy:
                result = adapter._run_pdf_inventory_gate(source_pdf, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("job_source_pdf_open_failed", result["reason"])
        structure_spy.assert_not_called()
        self.assertNotIn(str(source_pdf), result["reason"])

    def test_run_pdf_inventory_gate_rejects_symlink_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_source = root / "real.pdf"
            real_source.write_bytes(b"pdf")
            source_pdf = root / "source.pdf"
            source_pdf.symlink_to(real_source)
            snapshot = _FakeSnapshot(size=3, sha256=_sha256(b"pdf"))
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("must_not_run"),
            ) as structure_spy:
                result = adapter._run_pdf_inventory_gate(source_pdf, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("job_source_pdf_not_regular", result["reason"])
        structure_spy.assert_not_called()

    def test_run_pdf_inventory_gate_rejects_fifo_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_pdf = Path(tmp) / "source.pdf"
            os.mkfifo(source_pdf)
            snapshot = _FakeSnapshot(size=0, sha256=_sha256(b""))
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("must_not_run"),
            ) as structure_spy:
                result = adapter._run_pdf_inventory_gate(source_pdf, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("job_source_pdf_not_regular", result["reason"])
        structure_spy.assert_not_called()

    def test_run_pdf_inventory_gate_hash_mismatch_does_not_call_structure_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_pdf = Path(tmp) / "source.pdf"
            source_pdf.write_bytes(b"correct")
            snapshot = _FakeSnapshot(size=7, sha256=_sha256(b"incorrect"))
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("must_not_run"),
            ) as structure_spy:
                result = adapter._run_pdf_inventory_gate(source_pdf, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("job_source_pdf_sha256_mismatch", result["reason"])
        structure_spy.assert_not_called()
        self.assertNotIn(str(source_pdf), result["reason"])

    def test_run_pdf_inventory_gate_structure_exception_return_unavailable_and_still_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_pdf = Path(tmp) / "source.pdf"
            source_pdf.write_bytes(b"source")
            snapshot = _FakeSnapshot(size=6, sha256=_sha256(b"source"))
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("boom"),
            ):
                original_verify = adapter._verify_job_source
                verify_paths: list[Path] = []

                def spy_verify(
                    source_snapshot: object,
                    observed_path: Path,
                    *,
                    expected_identity: tuple[int, int] | None = None,
                ) -> tuple[int, int]:
                    verify_paths.append(observed_path)
                    return original_verify(
                        source_snapshot,
                        observed_path,
                        expected_identity=expected_identity,
                    )

                with patch.object(adapter, "_verify_job_source", side_effect=spy_verify):
                    result = adapter._run_pdf_inventory_gate(
                        source_pdf,
                        snapshot,
                    )
        self.assertFalse(result["available"])
        self.assertIn("RuntimeError", result["reason"])
        self.assertEqual(len(verify_paths), 2)
        self.assertEqual(verify_paths[0], source_pdf)
        self.assertEqual(verify_paths[1], source_pdf)
        self.assertEqual(snapshot.verify_calls, 2)

    def test_run_pdf_inventory_gate_rejects_path_not_named_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "other.pdf"
            source_pdf.write_bytes(b"pdf")
            snapshot = _FakeSnapshot(size=3, sha256=_sha256(b"pdf"))
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("must_not_run"),
            ) as structure_spy:
                result = adapter._run_pdf_inventory_gate(source_pdf, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("job_source_pdf_unexpected_name", result["reason"])
        structure_spy.assert_not_called()
        self.assertEqual(snapshot.verify_calls, 2)

    def test_run_pdf_inventory_gate_rejects_pdf_outside_expected_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "out"
            output_dir.mkdir()
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"pdf")
            snapshot = _FakeSnapshot(size=3, sha256=_sha256(b"pdf"))
            with patch.object(
                adapter,
                "pdf_structure_inventory",
                side_effect=RuntimeError("must_not_run"),
            ) as structure_spy:
                result = adapter._run_pdf_inventory_gate(
                    source_pdf,
                    snapshot,
                    expected_output_dir=output_dir,
                )
        self.assertFalse(result["available"])
        self.assertIn("job_source_pdf_unexpected_directory", result["reason"])
        structure_spy.assert_not_called()
        self.assertEqual(snapshot.verify_calls, 2)

    def test_evaluate_pdf_inventory_gate_allows_expected_high_counts_as_lower_bound(self) -> None:
        inventory = self._inventory(
            source_pdf_sha256="A" * 64,
            reason=None,
        )
        inventory["counts"] = {
            "table": {
                "high_confidence": 3,
                "ambiguous": 4,
                "records": [],
            },
            "algorithm": {
                "high_confidence": 1,
                "ambiguous": 2,
                "records": [],
            },
            "code": {
                "high_confidence": 0,
                "ambiguous": 0,
                "records": [],
            },
            "formula": {
                "high_confidence": 0,
                "ambiguous": 0,
                "records": [],
            },
        }
        structural = {
            "expected_tables": 7,
            "expected_algorithms": 1,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 1}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["structural"]["ok"])
        self.assertTrue(result["formula"]["ok"])
        self.assertEqual(result["actual_source_pdf_sha256"], "a" * 64)

    def test_evaluate_pdf_inventory_gate_formula_expected_zero_requires_no_formula_and_no_unknown_proof(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["formula"]["high_confidence"] = 0
        inventory["counts"]["formula"]["ambiguous"] = 0
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])

    def test_evaluate_pdf_inventory_gate_transformer_formula_count_one_allows_zero_high_confidence(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["formula"]["high_confidence"] = 0
        inventory["counts"]["formula"]["ambiguous"] = 7
        inventory["no_structure_proof"]["formula"] = adapter.KIND_UNKNOWN
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 1}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])

    def test_evaluate_pdf_inventory_gate_unknown_tokens_with_healthy_text_does_not_global_fail(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["reason"] = "unknown_tokens"
        inventory["counts"]["formula"]["high_confidence"] = 0
        inventory["counts"]["formula"]["ambiguous"] = 11
        inventory["text_health"]["reason"] = "unknown_tokens"
        inventory["no_structure_proof"]["formula"] = adapter.KIND_UNKNOWN
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 1}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["global_failure_reasons"], [])

    def test_evaluate_pdf_inventory_gate_bert_formula_zero_allows_safe_unknown_tokens_state(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["reason"] = "none"
        inventory["counts"]["formula"]["high_confidence"] = 0
        inventory["counts"]["formula"]["ambiguous"] = 0
        inventory["text_health"]["reason"] = "unknown_tokens"
        inventory["no_structure_proof"]["formula"] = adapter.KIND_HEALTHY
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["formula"]["failure_reasons"], [])

    def test_evaluate_pdf_inventory_gate_rejects_formula_zero_with_ambiguous_or_high(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["formula"]["high_confidence"] = 1
        inventory["counts"]["formula"]["ambiguous"] = 1
        inventory["no_structure_proof"]["formula"] = adapter.KIND_HEALTHY
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn("pdf_inventory_formula_unexpected_high:1", result["formula"]["failure_reasons"])
        self.assertIn("pdf_inventory_formula_unexpected_ambiguous:1", result["formula"]["failure_reasons"])

    def test_evaluate_pdf_inventory_gate_complex_table_example_with_lower_bound(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["table"]["high_confidence"] = 3
        structural = {
            "expected_tables": 7,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["structural"]["ok"])

    def test_evaluate_pdf_inventory_gate_real_world_25988_target_table_and_algorithm_counts(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["table"]["high_confidence"] = 12
        inventory["counts"]["algorithm"]["high_confidence"] = 1
        structural = {
            "expected_tables": 12,
            "expected_algorithms": 1,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["structural"]["failure_reasons"], [])

    def test_evaluate_pdf_inventory_gate_text_health_pages_not_list_fails_without_crash(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["text_health"] = {
            "available": True,
            "status": adapter.KIND_HEALTHY,
            "reason": None,
            "page_count": 1,
            "page_no_continuous": True,
            "pages": {"page_no": 1},
        }
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn("pdf_inventory_text_health_pages_invalid", result["global_failure_reasons"])

    def test_evaluate_pdf_inventory_gate_text_health_pages_with_invalid_entry_fails_without_crash(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["text_health"]["pages"] = [1, {"page_no": 1, "healthy": True, "reasons": []}]
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_text_health_page_invalid",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_non_bool_page_no_continuous(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["text_health"]["page_no_continuous"] = "false"
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_text_health_page_sequence_broken",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_text_health_page_count_mismatch(self) -> None:
        inventory = self._inventory(page_count=1, source_pdf_sha256="A" * 64)
        inventory["text_health"]["page_count"] = 2
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_text_health_page_count_mismatch",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_malformed_reason_schema(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["reason"] = {"unexpected": "object"}
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            {"formula_count": 0},
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_reason_invalid",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_malformed_record_schema(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["table"]["records"] = "not-a-list"
        inventory["counts"]["formula"]["record_count"] = "bad"
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            {"formula_count": 0},
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_records_invalid:table",
            result["structural_failure_reasons"],
        )
        self.assertIn(
            "pdf_inventory_records_invalid:formula",
            result["formula_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_non_mapping_payload(self) -> None:
        structural = {
            "expected_tables": 0,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        result = adapter._evaluate_pdf_inventory_gate(
            [],  # type: ignore[arg-type]
            structural,
            {"formula_count": 0},
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_schema_invalid",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_invalid_page_no_or_health_in_pages(self) -> None:
        inventory = self._inventory(page_count=1, source_pdf_sha256="A" * 64)
        inventory["text_health"]["pages"] = [
            {"page_no": 999, "healthy": True, "reasons": []},
            {"page_no": 1, "healthy": True, "reasons": []},
        ]
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_text_health_page_no_out_of_range",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_duplicate_page_no(self) -> None:
        inventory = self._inventory(page_count=2, source_pdf_sha256="A" * 64)
        inventory["text_health"]["pages"] = [
            {"page_no": 1, "healthy": True, "reasons": []},
            {"page_no": 1, "healthy": True, "reasons": []},
        ]
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_text_health_pages_non_continuous",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_rejects_extra_pages(self) -> None:
        inventory = self._inventory(page_count=1, source_pdf_sha256="A" * 64)
        inventory["text_health"]["pages"] = [
            {"page_no": 1, "healthy": True, "reasons": []},
            {"page_no": 2, "healthy": True, "reasons": []},
        ]
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "pdf_inventory_text_health_pages_missing",
            result["global_failure_reasons"],
        )

    def test_evaluate_pdf_inventory_gate_truncates_text_health_pages(self) -> None:
        inventory = self._inventory(page_count=60, source_pdf_sha256="A" * 64)
        inventory["text_health"]["pages"] = [
            {
                "page_no": index,
                "healthy": True,
                "reasons": [],
            }
            for index in range(1, 61)
        ]
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["text_health"]["truncated_pages"])
        self.assertEqual(len(result["text_health"]["pages"]), 50)
        self.assertEqual(result["text_health"]["page_count"], 60)

    def test_evaluate_pdf_inventory_gate_missing_text_health_pages_fails(self) -> None:
        inventory = self._inventory(page_count=2, source_pdf_sha256="A" * 64)
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn("pdf_inventory_text_health_pages_missing", result["global_failure_reasons"])

    def test_evaluate_pdf_inventory_gate_truncates_inventory_records(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"]["table"]["high_confidence"] = 7
        inventory["counts"]["table"]["records"] = [
            {"text": str(index), "page_no": 1, "confidence": "high", "source": "document.json"}
            for index in range(25)
        ]
        structural = {
            "expected_tables": 7,
            "expected_algorithms": 0,
            "expected_code_blocks": 0,
        }
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertTrue(result["ok"])
        summary = result["counts"]["table"]
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["record_count"], 25)
        self.assertEqual(len(summary["records"]), 20)

    def test_evaluate_pdf_inventory_gate_ignores_malicious_document_counts(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["document_counts"] = {"table": {"records": [{"path": "/tmp/sensitive/path"}]}}
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertNotIn("document_counts", result)

    def test_evaluate_pdf_inventory_gate_fails_for_missing_expected_counts(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        structural = {"expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn("pdf_inventory_expected_count_missing:table", result["structural_failure_reasons"])

    def test_evaluate_pdf_inventory_gate_fails_for_invalid_expected_counts(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        structural = {"expected_tables": "seven", "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": "none"}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn("pdf_inventory_expected_count_invalid:table", result["structural_failure_reasons"])
        self.assertIn("pdf_inventory_expected_count_invalid:formula", result["formula_failure_reasons"])

    def test_evaluate_pdf_inventory_gate_late_global_reasons_apply_to_structural_and_formula(self) -> None:
        inventory = self._inventory(source_pdf_sha256="A" * 64)
        inventory["counts"] = object()  # type: ignore[assignment]
        structural = {"expected_tables": 0, "expected_algorithms": 0, "expected_code_blocks": 0}
        formula = {"formula_count": 0}
        result = adapter._evaluate_pdf_inventory_gate(
            inventory,
            structural,
            formula,
            expected_source_pdf_sha256="a" * 64,
        )
        self.assertIn("pdf_inventory_counts_missing", result["global_failure_reasons"])
        self.assertIn("pdf_inventory_counts_missing", result["structural_failure_reasons"])
        self.assertIn("pdf_inventory_counts_missing", result["formula_failure_reasons"])

    def test_finalize_delivery_surfaces_writes_pdf_structure_inventory_and_runs_before_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "document.json").write_text("{}", encoding="utf-8")
            metadata: dict[str, Any] = {}
            status: dict[str, Any] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            events: list[str] = []

            def fake_record(
                _output_dir: Path,
                _metadata: dict[str, Any],
                _status: dict[str, Any],
                _args: Namespace,
            ) -> None:
                events.append("record")

            def fake_restore(
                _output_dir: Path,
                _document: dict[str, Any],
                _source_pdf_path: Path,
                _metadata: dict[str, Any],
                _status: dict[str, Any],
                *,
                visual_pdf_path: Path,
            ) -> dict[str, Any]:
                events.append("restore")
                return {"table_source_candidate_count": 0}

            def fake_formula(*_args: object, **_kwargs: object) -> dict[str, Any]:
                events.append("formula")
                return {"ok": True}

            def fake_structural(*_args: object, **_kwargs: object) -> dict[str, Any]:
                events.append("structural")
                return {"ok": True}

            def fake_inventory(*_args: object, **_kwargs: object) -> dict[str, Any]:
                events.append("inventory")
                return {
                    "ok": True,
                    "source_pdf": "source.pdf",
                    "expected_source_pdf_sha256": "a" * 64,
                    "actual_source_pdf_sha256": "a" * 64,
                    "failure_reasons": [],
                    "global_failure_reasons": [],
                    "structural_failure_reasons": [],
                    "formula_failure_reasons": [],
                    "counts": {kind: {"high_confidence": 0, "ambiguous": 0, "records": [], "record_count": 0, "truncated": False} for kind in adapter.KIND_ORDER},
                    "proof": {kind: adapter.KIND_HEALTHY for kind in adapter.KIND_ORDER},
                    "structural": {"ok": True, "failure_reasons": []},
                    "formula": {"ok": True, "failure_reasons": []},
                }

            def fake_reconcile(*_args: object, **_kwargs: object) -> dict[str, Any]:
                events.append("reconcile")
                return {"ok": True}

            with (
                patch.object(adapter, "record_cn_accepted_baseline", side_effect=fake_record),
                patch.object(adapter, "restore_final_delivery_visuals", side_effect=fake_restore),
                patch.object(adapter, "validate_final_formula_surfaces", side_effect=fake_formula),
                patch.object(adapter, "validate_final_structural_surfaces", side_effect=fake_structural),
                patch.object(adapter, "_evaluate_pdf_inventory_gate", side_effect=fake_inventory),
                patch.object(adapter, "reconcile_final_surface_status", side_effect=fake_reconcile),
            ):
                result = adapter._finalize_delivery_surfaces(
                    output_dir,
                    {},
                    output_dir / "source.pdf",
                    output_dir / "visual.pdf",
                    metadata,
                    status,
                    Namespace(),
                    pdf_inventory={"available": True},
                )

        self.assertEqual(events, ["record", "restore", "formula", "structural", "inventory", "reconcile"])
        self.assertIn("pdf_structure_inventory", metadata)
        self.assertIn("final_pdf_inventory", metadata)
        self.assertIn("pdf_structure_inventory", status["quality_signals"])
        self.assertIn("final_pdf_inventory", status["quality_signals"])
        self.assertIn("pdf_inventory", result)

    def test_finalize_delivery_surfaces_legacy_inventory_bypass_marks_applied_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "document.json").write_text("{}", encoding="utf-8")
            (output_dir / "document.html").write_text("", encoding="utf-8")
            (output_dir / "document.md").write_text("", encoding="utf-8")
            metadata: dict[str, Any] = {"source_pdf_sha256": "a" * 64}
            status: dict[str, Any] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }

            def fake_record(
                _output_dir: Path,
                _metadata: dict[str, Any],
                _status: dict[str, Any],
                _args: Namespace,
            ) -> None:
                return None

            def fake_restore(
                _output_dir: Path,
                _document: dict[str, Any],
                _source_pdf_path: Path,
                _metadata: dict[str, Any],
                _status: dict[str, Any],
                *,
                visual_pdf_path: Path,
            ) -> dict[str, Any]:
                return {"table_source_candidate_count": 0}

            def fake_formula(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {"ok": True}

            def fake_structural(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {"ok": True}

            def fake_reconcile(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {"ok": True}

            result = adapter._finalize_delivery_surfaces(
                output_dir,
                {},
                output_dir / "source.pdf",
                output_dir / "visual.pdf",
                metadata,
                status,
                Namespace(),
                pdf_inventory=None,
            )

            self.assertEqual(result["pdf_inventory"]["applied"], False)
            self.assertTrue(result["pdf_inventory"]["ok"])
            self.assertIn("pdf_structure_inventory", metadata)
            self.assertEqual(metadata["pdf_structure_inventory"]["applied"], False)
            self.assertIn("final_pdf_inventory", metadata)

    def test_finalize_delivery_surfaces_failing_inventory_sets_overall_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "document.json").write_text("{}", encoding="utf-8")
            metadata: dict[str, Any] = {}
            status: dict[str, Any] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }

            def fake_record(
                _output_dir: Path,
                _metadata: dict[str, Any],
                _status: dict[str, Any],
                _args: Namespace,
            ) -> None:
                return None

            def fake_restore(
                _output_dir: Path,
                _document: dict[str, Any],
                _source_pdf_path: Path,
                _metadata: dict[str, Any],
                _status: dict[str, Any],
                *,
                visual_pdf_path: Path,
            ) -> dict[str, Any]:
                return {"table_source_candidate_count": 0}

            def fake_formula(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {"ok": True}

            def fake_structural(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {"ok": True}

            def fake_inventory(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {
                    "ok": False,
                    "source_pdf": "source.pdf",
                    "failure_reasons": [
                        "pdf_inventory_counts_missing"
                    ],
                    "global_failure_reasons": ["pdf_inventory_counts_missing"],
                    "structural_failure_reasons": ["pdf_inventory_counts_missing"],
                    "formula_failure_reasons": ["pdf_inventory_counts_missing"],
                    "structural": {"ok": False, "failure_reasons": ["pdf_inventory_counts_missing"]},
                    "formula": {"ok": False, "failure_reasons": ["pdf_inventory_counts_missing"]},
                }

            def fake_reconcile(*_args: object, **_kwargs: object) -> dict[str, Any]:
                return {"ok": True}

            with (
                patch.object(adapter, "record_cn_accepted_baseline", side_effect=fake_record),
                patch.object(adapter, "restore_final_delivery_visuals", side_effect=fake_restore),
                patch.object(adapter, "validate_final_formula_surfaces", side_effect=fake_formula),
                patch.object(adapter, "validate_final_structural_surfaces", side_effect=fake_structural),
                patch.object(adapter, "_evaluate_pdf_inventory_gate", side_effect=fake_inventory),
                patch.object(adapter, "reconcile_final_surface_status", side_effect=fake_reconcile),
            ):
                result = adapter._finalize_delivery_surfaces(
                    output_dir,
                    {},
                    output_dir / "source.pdf",
                    output_dir / "visual.pdf",
                    metadata,
                    status,
                    Namespace(),
                    pdf_inventory={"available": True},
                )

        self.assertFalse(status["ok"])
        self.assertEqual(status["success_class"], "degraded_failure")
        self.assertEqual(
            [warning for warning in status["warnings"] if warning.startswith("final_pdf_inventory_failed")],
            ["final_pdf_inventory_failed:pdf_inventory_counts_missing"],
        )
        self.assertFalse(result["pdf_inventory"]["ok"])
        self.assertFalse(result["pdf_inventory"]["structural"]["ok"])
        self.assertFalse(result["pdf_inventory"]["formula"]["ok"])
        self.assertFalse(result["formula"]["ok"])
        self.assertFalse(result["structural"]["ok"])
        self.assertFalse(status["quality_signals"]["final_formula_surface"]["ok"])
        self.assertFalse(status["quality_signals"]["final_structural_surface"]["ok"])
        self.assertIn(
            "pdf_inventory_counts_missing",
            status.get("quality_signals", {}).get("final_formula_surface", {}).get(
                "failure_reasons",
                [],
            ),
        )
        self.assertIn(
            "pdf_inventory_counts_missing",
            status.get("quality_signals", {}).get("final_structural_surface", {}).get(
                "failure_reasons",
                [],
            ),
        )
        self.assertFalse(metadata["final_formula_surface"]["ok"])
        self.assertFalse(metadata["final_structural_surface"]["ok"])
