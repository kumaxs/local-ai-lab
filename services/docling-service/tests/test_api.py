from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from docling_service.release import JobManager, ReleaseConfig


HTTP_AVAILABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("fastapi", "multipart", "httpx")
)


@unittest.skipUnless(HTTP_AVAILABLE, "HTTP release dependencies are not installed")
class ApiTests(unittest.TestCase):
    def test_authenticated_pdf_job_and_output_download(self) -> None:
        from fastapi.testclient import TestClient

        from docling_service.api import create_app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "adapter.py"
            adapter.write_text("# test\n", encoding="utf-8")
            config = ReleaseConfig(
                profile="docker",
                serve_url="http://backend:5001",
                adapter_path=adapter,
                input_root=root / "inputs",
                output_root=root / "outputs",
                state_root=root / "state",
                max_upload_bytes=1024,
                max_concurrent_jobs=1,
                conversion_timeout_seconds=60,
                image_export_mode="referenced",
                formula_policy="formula_service",
                cn_ocr_parity=False,
                api_token="test-token",
                formula_ocr_url="http://formula:8001",
            )

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_dir = config.output_root / job_id
                output_dir.mkdir(parents=True)
                (output_dir / "document.md").write_text("# Converted\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            manager = JobManager(config, runner=runner)
            app = create_app(config, manager)
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(app) as client:
                self.assertEqual(401, client.get("/v1/capabilities").status_code)
                response = client.post(
                    "/v1/jobs",
                    headers=headers,
                    files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
                )
                self.assertEqual(202, response.status_code)
                job_id = response.json()["job_id"]
                for _attempt in range(100):
                    status = client.get(f"/v1/jobs/{job_id}", headers=headers)
                    if status.json()["state"] == "succeeded":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("job did not finish")
                outputs = client.get(f"/v1/jobs/{job_id}/outputs", headers=headers)
                self.assertEqual("document.md", outputs.json()["files"][0]["path"])
                download = client.get(
                    f"/v1/jobs/{job_id}/files/document.md", headers=headers
                )
                self.assertEqual("# Converted\n", download.text)


if __name__ == "__main__":
    unittest.main()
