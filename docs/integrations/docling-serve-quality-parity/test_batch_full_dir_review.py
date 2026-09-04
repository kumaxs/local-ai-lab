from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import batch_full_dir_review as review  # noqa: E402


def _args(
    input_dir: Path,
    output_root: Path,
    *,
    expected_count: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_dir=input_dir,
        output_root=output_root,
        expected_count=expected_count,
        serve_url="http://127.0.0.1:5001",
        adapter=Path("quality_parity_adapter.py"),
        timeout_seconds=1,
        http_retries=0,
        python=sys.executable,
        formula_second_pass_policy="off",
        formula_second_pass_route_b_root=None,
        formula_second_pass_review_candidate_root=[],
        formula_second_pass_guarded_fallback_root=[],
        formula_second_pass_guarded_fallback_eq=[],
        cn_ocr_parity=False,
        cn_ocr_request_shape="preset",
        cn_ocr_chunk_size=1,
    )


def _write_success(cmd: list[str], *, ok: object = True) -> None:
    output_root = Path(cmd[cmd.index("--output-root") + 1])
    job_id = cmd[cmd.index("--job-id") + 1]
    output_dir = output_root / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "status.json").write_text(
        json.dumps({"ok": ok, "success_class": "success"}),
        encoding="utf-8",
    )
    (output_dir / "metadata.json").write_text("{}", encoding="utf-8")


class BatchPreflightTests(unittest.TestCase):
    def test_missing_and_empty_input_return_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = _args(root / "missing", root / "out-missing")
            empty = root / "empty"
            empty.mkdir()
            empty_args = _args(empty, root / "out-empty")
            for args in (missing, empty_args):
                stderr = io.StringIO()
                with patch("sys.stderr", stderr):
                    self.assertEqual(review.run_batch(args), 2)
                self.assertIn("preflight", stderr.getvalue())

    def test_uppercase_pdf_suffix_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "One.PDF").write_bytes(b"one")
            pdfs = review._discover_pdfs(input_dir)
            self.assertEqual([path.name for path in pdfs], ["One.PDF"])

    def test_expected_count_mismatch_and_duplicate_content_return_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            first = input_dir / "one.pdf"
            second = input_dir / "two.pdf"
            first.write_bytes(b"same")
            second.write_bytes(b"same")

            mismatch = _args(input_dir, root / "out-mismatch", expected_count=3)
            self.assertEqual(review.run_batch(mismatch), 2)
            duplicate = _args(input_dir, root / "out-duplicate")
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                self.assertEqual(review.run_batch(duplicate), 2)
            self.assertIn("duplicate", stderr.getvalue())

    def test_casefold_job_id_collision_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "Report A.pdf").write_bytes(b"one")
            (input_dir / "Report-A.pdf").write_bytes(b"two")
            args = _args(input_dir, root / "out")
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                self.assertEqual(review.run_batch(args), 2)
            self.assertIn("collision", stderr.getvalue())
            self.assertFalse((root / "out").exists())

    def test_nonempty_and_symlink_output_roots_are_rejected_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "sample.pdf").write_bytes(b"sample")

            nonempty = root / "nonempty"
            nonempty.mkdir()
            marker = nonempty / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            self.assertEqual(review.run_batch(_args(input_dir, nonempty)), 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

            target = root / "target"
            target.mkdir()
            symlink = root / "symlink"
            os.symlink(target, symlink)
            self.assertEqual(review.run_batch(_args(input_dir, symlink)), 2)
            self.assertTrue(symlink.is_symlink())
            self.assertEqual(list(target.iterdir()), [])

    def test_duplicate_symlink_pdf_is_not_accepted_as_the_only_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            source = root / "source.pdf"
            source.write_bytes(b"source")
            os.symlink(source, input_dir / "linked.pdf")
            self.assertEqual(review.run_batch(_args(input_dir, root / "out")), 2)

    def test_any_pdf_named_symlink_is_rejected_even_with_a_regular_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "regular.pdf").write_bytes(b"regular")
            source = root / "source.pdf"
            source.write_bytes(b"source")
            os.symlink(source, input_dir / "linked.pdf")
            self.assertEqual(review.run_batch(_args(input_dir, root / "out")), 2)

    def test_any_pdf_named_nonregular_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "regular.pdf").write_bytes(b"regular")
            (input_dir / "directory.pdf").mkdir()
            self.assertEqual(review.run_batch(_args(input_dir, root / "out")), 2)

    def test_safe_job_id_strips_dangerous_edge_punctuation(self) -> None:
        self.assertEqual(review.safe_job_id(Path("._-Report-._-.pdf")), "Report")
        self.assertEqual(review.safe_job_id(Path("._-.pdf")), "document")

    def test_long_pdf_name_gets_bounded_hashed_job_id_and_capture_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            stem = "A" * 240
            sample = input_dir / f"{stem}.pdf"
            sample.write_bytes(b"long-name")
            job_id = review.safe_job_id(sample)
            expected_suffix = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
            self.assertLessEqual(len(job_id), review.MAX_JOB_ID_LENGTH)
            self.assertTrue(job_id.endswith(f"-{expected_suffix}"))

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_success(cmd)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            output_root = root / "out"
            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 0)
            capture = output_root / f"{job_id}.adapter_stdout.json"
            self.assertTrue(capture.exists())
            self.assertLess(len(capture.name), 255)

    def test_programmatic_numeric_validation_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "sample.pdf").write_bytes(b"sample")
            for field, value in (
                ("timeout_seconds", 0),
                ("http_retries", -1),
                ("cn_ocr_chunk_size", 0),
                ("expected_count", 0),
            ):
                args = _args(input_dir, root / f"out-{field}")
                setattr(args, field, value)
                self.assertEqual(review.run_batch(args), 2, field)

    def test_cli_numeric_validation_exits_two(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "batch_full_dir_review.py",
                "--output-root",
                "/tmp/out",
                "--adapter",
                "/tmp/adapter.py",
                "--timeout-seconds",
                "0",
            ],
        ):
            with self.assertRaises(SystemExit) as raised:
                review.parse_args()
            self.assertEqual(raised.exception.code, 2)

    def test_cli_default_matches_release_formula_policy_off(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "batch_full_dir_review.py",
                "--output-root",
                "/tmp/out",
                "--adapter",
                "/tmp/adapter.py",
            ],
        ):
            args = review.parse_args()
        self.assertEqual(args.formula_second_pass_policy, "off")
        command = review._build_adapter_command(
            args,
            Path("/tmp/input.pdf"),
            Path("/tmp/out"),
            "input",
            input_sha256="a" * 64,
        )
        self.assertNotIn("--formula-second-pass-policy", command)


class BatchExecutionTests(unittest.TestCase):
    def test_timeout_bytes_is_recorded_and_batch_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "first.pdf").write_bytes(b"first")
            (input_dir / "second.pdf").write_bytes(b"second")
            output_root = root / "out"
            calls: list[list[str]] = []

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(
                        cmd,
                        timeout=1,
                        output=b"stdout-\xff",
                        stderr=b"stderr-\xfe",
                    )
                _write_success(cmd)
                return SimpleNamespace(returncode=0, stdout=None, stderr=None)

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)

            rows = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["success_class"], "timeout")
            self.assertTrue(rows[1]["ok"])
            self.assertIn("stdout-", (output_root / "first.adapter_stdout.json").read_text())
            self.assertIn("stderr-", (output_root / "first.adapter_stderr.txt").read_text())

    def test_oserror_is_recorded_and_batch_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "first.pdf").write_bytes(b"first")
            (input_dir / "second.pdf").write_bytes(b"second")
            output_root = root / "out"
            calls: list[list[str]] = []

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                if len(calls) == 1:
                    raise OSError("worker missing")
                _write_success(cmd)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            rows = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertIn("subprocess error", rows[0]["failure_reason"])
            self.assertTrue(rows[1]["ok"])
            self.assertEqual(len(calls), 2)

    def test_status_ok_string_is_failure_and_aggregate_exit_is_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "sample.pdf").write_bytes(b"sample")
            output_root = root / "out"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_success(cmd, ok="true")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            rows = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertIs(rows[0]["ok"], False)
            self.assertIn("JSON boolean true", rows[0]["failure_reason"])

    def test_structured_status_errors_are_textualized_without_malformed_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "sample.pdf").write_bytes(b"sample")
            output_root = root / "out"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_success(cmd)
                job_id = cmd[cmd.index("--job-id") + 1]
                status_path = output_root / job_id / "status.json"
                status_path.write_text(
                    json.dumps(
                        {
                            "ok": False,
                            "errors": [{"code": "E_PARSE", "detail": "bad|value"}],
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            rows = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertFalse(rows[0]["ok"])
            self.assertIn('"code": "E_PARSE"', rows[0]["failure_reason"])
            self.assertNotIn("output summary error", rows[0]["failure_reason"])

    def test_input_change_is_failed_without_adapter_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            sample = input_dir / "sample.pdf"
            sample.write_bytes(b"before")
            output_root = root / "out"
            original = review._fingerprint_pdf
            fingerprint_calls = 0

            def fingerprint(path: Path) -> dict[str, object]:
                nonlocal fingerprint_calls
                fingerprint_calls += 1
                result = original(path)
                if fingerprint_calls == 1:
                    path.write_bytes(b"after")
                return result

            with (
                patch.object(review, "_fingerprint_pdf", side_effect=fingerprint),
                patch.object(review.subprocess, "run") as run,
            ):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            run.assert_not_called()
            rows = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertIn("input_changed_after_preflight", rows[0]["failure_reason"])
            self.assertEqual(rows[0]["input_size_bytes"], len(b"before"))

    def test_preflight_sha_is_forwarded_to_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            sample = input_dir / "sample.pdf"
            content = b"sha-bound"
            sample.write_bytes(content)
            expected_sha = hashlib.sha256(content).hexdigest()
            output_root = root / "out"
            seen: list[str] = []

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                seen.extend(cmd)
                _write_success(cmd)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 0)
            self.assertEqual(
                seen[seen.index("--expected-input-sha256") + 1],
                expected_sha,
            )

    def test_roster_addition_stops_without_new_summary_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "first.pdf").write_bytes(b"first")
            (input_dir / "second.pdf").write_bytes(b"second")
            output_root = root / "out"
            calls: list[list[str]] = []

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                _write_success(cmd)
                (input_dir / "added.pdf").write_bytes(b"added")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            rows = json.loads((output_root / "run_summary.json").read_text())
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(row["ok"] is False for row in rows))

    def test_roster_deletion_stops_without_new_summary_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            (input_dir / "first.pdf").write_bytes(b"first")
            second = input_dir / "second.pdf"
            second.write_bytes(b"second")
            output_root = root / "out"
            calls: list[list[str]] = []

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                _write_success(cmd)
                second.unlink()
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            rows = json.loads((output_root / "run_summary.json").read_text())
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(row["ok"] is False for row in rows))

    def test_batch_end_detects_input_change_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "inputs"
            input_dir.mkdir()
            sample = input_dir / "sample.pdf"
            sample.write_bytes(b"before")
            output_root = root / "out"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_success(cmd)
                sample.write_bytes(b"after")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(review.subprocess, "run", side_effect=fake_run):
                self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
            rows = json.loads((output_root / "run_summary.json").read_text())
            self.assertIn("corpus_input_changed", rows[0]["failure_reason"])

    def test_malformed_artifacts_fail_current_pdf_and_continue(self) -> None:
        malformed = (
            ("status-list", lambda output: (output / "status.json").write_text("[]")),
            ("metadata-scalar", lambda output: (output / "metadata.json").write_text("[]")),
            ("bad-utf8", lambda output: (output / "metadata.json").write_bytes(b"\\xff")),
            (
                "nested",
                lambda output: (
                    (output / "metadata.json").write_text(
                        json.dumps({"formula_second_pass": []})
                    )
                ),
            ),
            (
                "warning-type",
                lambda output: (
                    (output / "status.json").write_text(
                        json.dumps({"ok": True, "warnings": {"bad": "shape"}})
                    )
                ),
            ),
            (
                "diagnostics-item-type",
                lambda output: (
                    (output / "metadata.json").write_text(
                        json.dumps({"formula_number_qc_diagnostics": ["bad"]})
                    )
                ),
            ),
            (
                "status-directory",
                lambda output: (
                    (output / "status.json").unlink(),
                    (output / "status.json").mkdir(),
                ),
            ),
        )
        for name, mutate in malformed:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                input_dir = root / "inputs"
                input_dir.mkdir()
                (input_dir / "first.pdf").write_bytes(f"{name}-first".encode())
                (input_dir / "second.pdf").write_bytes(f"{name}-second".encode())
                output_root = root / "out"
                calls: list[list[str]] = []

                def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                    calls.append(cmd)
                    _write_success(cmd)
                    if len(calls) == 1:
                        mutate(output_root / cmd[cmd.index("--job-id") + 1])
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with patch.object(review.subprocess, "run", side_effect=fake_run):
                    self.assertEqual(review.run_batch(_args(input_dir, output_root)), 1)
                rows = json.loads((output_root / "run_summary.json").read_text())
                self.assertEqual(len(rows), 2)
                self.assertFalse(rows[0]["ok"])
                self.assertTrue(rows[1]["ok"])


class BatchReportingTests(unittest.TestCase):
    def test_integrity_failure_preserves_timeout_and_deduplicates_reason(self) -> None:
        rows = [{"ok": False, "success_class": "timeout", "failure_reason": "timeout"}]
        review._mark_batch_integrity_failure(rows, "corpus changed")
        review._mark_batch_integrity_failure(rows, "corpus changed")
        self.assertIs(rows[0]["ok"], False)
        self.assertEqual(rows[0]["success_class"], "timeout")
        self.assertEqual(rows[0]["failure_reason"], "timeout; corpus changed")

    def test_output_root_lock_is_exclusive_and_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "out"
            root.mkdir()
            first = review.OutputRootLock(root).acquire()
            try:
                with self.assertRaises(review.PreflightError):
                    review.OutputRootLock(root).acquire()
            finally:
                first.release()
            second = review.OutputRootLock(root).acquire()
            second.release()
            self.assertFalse(second.path.exists())

    def test_crafted_atomic_temp_name_does_not_get_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "summary.json"
            crafted = root / ".summary.json.tmp"
            crafted.write_text("attacker", encoding="utf-8")
            review._atomic_write_text(target, "safe")
            self.assertEqual(target.read_text(encoding="utf-8"), "safe")
            self.assertEqual(crafted.read_text(encoding="utf-8"), "attacker")
            leftovers = list(root.glob(".summary.json.*.tmp"))
            self.assertEqual(leftovers, [])

    def test_relative_input_is_reported_as_absolute_and_fingerprint_fields_are_present(self) -> None:
        row = review.summarize_failure(
            Path("relative.pdf"),
            "relative",
            Path("out") / "relative",
            0.1,
            "failed",
            False,
            input_size_bytes=12,
            input_sha256="abc",
        )
        self.assertTrue(Path(row["input_path"]).is_absolute())
        self.assertEqual(row["input_size_bytes"], 12)
        self.assertEqual(row["input_sha256"], "abc")

    def test_markdown_dynamic_fields_escape_pipe_and_line_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            row = review.summarize_failure(
                Path("/tmp/name"),
                "name",
                output_root / "name",
                0.1,
                "bad|line\r\nnext",
                False,
            )
            row["input_filename"] = "paper|name\npart.pdf"
            row["output_dir"] = "/tmp/out|root\npart"
            review.write_markdown_summary(output_root, [row])
            content = (output_root / "run_summary.md").read_text(encoding="utf-8")
            self.assertIn(r"paper\|name part.pdf", content)
            self.assertIn(r"bad\|line  next", content)
            self.assertNotIn("paper|name\npart.pdf", content)

    def test_summary_json_remains_a_list_and_atomic_temps_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            output_root.mkdir(exist_ok=True)
            row = review.summarize_failure(
                Path("/tmp/name"),
                "name",
                output_root / "name",
                0.1,
                "failed",
                False,
            )
            review._refresh_summaries(output_root, [row])
            parsed = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertIsInstance(parsed, list)
            self.assertFalse((output_root / ".run_summary.json.tmp").exists())
            self.assertFalse((output_root / ".run_summary.md.tmp").exists())


if __name__ == "__main__":
    unittest.main()
