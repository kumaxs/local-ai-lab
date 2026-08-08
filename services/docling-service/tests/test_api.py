from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tempfile
import time
import unittest
import zipfile
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
                webhook_allowed_hosts=("localhost",),
                webhook_allow_private_hosts=True,
            )

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_root = Path(command[command.index("--output-root") + 1])
                output_dir = output_root / job_id
                output_dir.mkdir(parents=True)
                (output_dir / "document.html").write_text("<p>Converted</p>", encoding="utf-8")
                (output_dir / "document.md").write_text("# Converted\n", encoding="utf-8")
                (output_dir / "document.json").write_text("{}", encoding="utf-8")
                (output_dir / "metadata.json").write_text("{}", encoding="utf-8")
                (output_dir / "status.json").write_text(
                    json.dumps({"ok": True}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "{}", "")

            manager = JobManager(config, runner=runner)
            app = create_app(config, manager)
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(app) as client:
                unauthorized = client.get("/v1/capabilities")
                self.assertEqual(401, unauthorized.status_code)
                self.assertTrue(
                    unauthorized.headers["content-type"].startswith(
                        "application/problem+json"
                    )
                )
                self.assertEqual("Bearer", unauthorized.headers["www-authenticate"])
                missing_route = client.get("/v1/does-not-exist", headers=headers)
                self.assertEqual(404, missing_route.status_code)
                self.assertTrue(
                    missing_route.headers["content-type"].startswith(
                        "application/problem+json"
                    )
                )
                wrong_method = client.put("/v1/capabilities", headers=headers)
                self.assertEqual(405, wrong_method.status_code)
                self.assertTrue(
                    wrong_method.headers["content-type"].startswith(
                        "application/problem+json"
                    )
                )
                openapi = client.get("/openapi.json").json()
                self.assertEqual("3.1.0", openapi["openapi"])
                self.assertIn("webhooks", openapi)
                webhook_content = openapi["webhooks"]["docling-job-event"][
                    "post"
                ]["requestBody"]["content"]
                self.assertIn("application/cloudevents+json", webhook_content)
                self.assertNotIn("application/json", webhook_content)
                self.assertIn("ProblemDetails", openapi["components"]["schemas"])
                upload_schema = openapi["paths"]["/v1/jobs"]["post"][
                    "requestBody"
                ]["content"]["multipart/form-data"]["schema"]
                self.assertNotIn("$ref", upload_schema)
                self.assertEqual("binary", upload_schema["properties"]["file"]["format"])
                file_content = openapi["paths"][
                    "/v1/jobs/{job_id}/files/{relative_path}"
                ]["get"]["responses"]["200"]["content"]
                self.assertIn("application/octet-stream", file_content)
                archive_content = openapi["paths"]["/v1/jobs/{job_id}/archive"][
                    "get"
                ]["responses"]["200"]["content"]
                self.assertIn("application/zip", archive_content)
                oversized_request = client.post(
                    "/v1/jobs",
                    headers={
                        **headers,
                        "Content-Type": "multipart/form-data; boundary=oversized",
                    },
                    content=b"x" * (1024 * 1024 + 2048),
                )
                self.assertEqual(413, oversized_request.status_code)
                self.assertEqual(
                    "request_too_large", oversized_request.json()["code"]
                )
                empty_idempotency = client.post(
                    "/v1/jobs",
                    headers={**headers, "Idempotency-Key": "   "},
                    files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
                )
                self.assertEqual(422, empty_idempotency.status_code)
                response = client.post(
                    "/v1/jobs",
                    headers={**headers, "Idempotency-Key": "api-test-1"},
                    files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
                )
                self.assertEqual(202, response.status_code)
                job_id = response.json()["job_id"]
                replay = client.post(
                    "/v1/jobs",
                    headers={**headers, "Idempotency-Key": "api-test-1"},
                    files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
                )
                self.assertEqual(job_id, replay.json()["job_id"])
                self.assertTrue(replay.json()["idempotent_replay"])
                conflict = client.post(
                    "/v1/jobs",
                    headers={**headers, "Idempotency-Key": "api-test-1"},
                    data={"client_reference": "different-request"},
                    files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
                )
                self.assertEqual(409, conflict.status_code)
                for _attempt in range(100):
                    status = client.get(f"/v1/jobs/{job_id}", headers=headers)
                    if status.json()["state"] == "succeeded":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("job did not finish")
                outputs = client.get(f"/v1/jobs/{job_id}/outputs", headers=headers)
                self.assertIn(
                    "document.md",
                    [item["path"] for item in outputs.json()["files"]],
                )
                download = client.get(
                    f"/v1/jobs/{job_id}/files/document.md", headers=headers
                )
                self.assertEqual("# Converted\n", download.text)
                late_file = root / "outputs" / job_id / "unpublished.txt"
                late_file.write_text("not in the committed manifest", encoding="utf-8")
                unpublished = client.get(
                    f"/v1/jobs/{job_id}/files/unpublished.txt", headers=headers
                )
                self.assertEqual(404, unpublished.status_code)
                listing = client.get("/v1/jobs", headers=headers).json()
                self.assertEqual(job_id, listing["items"][0]["job_id"])
                archive = client.get(f"/v1/jobs/{job_id}/archive", headers=headers)
                self.assertEqual(200, archive.status_code)
                with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                    self.assertIn("manifest.json", bundle.namelist())
                    self.assertIn("document.md", bundle.namelist())
                    self.assertNotIn("source.pdf", bundle.namelist())
                (root / "outputs" / job_id / "document.md").write_text(
                    "tampered", encoding="utf-8"
                )
                corrupted_file = client.get(
                    f"/v1/jobs/{job_id}/files/document.md", headers=headers
                )
                self.assertEqual(409, corrupted_file.status_code)
                corrupted_archive = client.get(
                    f"/v1/jobs/{job_id}/archive", headers=headers
                )
                self.assertEqual(409, corrupted_archive.status_code)
                storage = client.get("/v1/system/storage", headers=headers)
                self.assertEqual(200, storage.status_code)
                self.assertIn("total_managed_bytes", storage.json()["usage"])
                subscription = client.post(
                    "/v1/webhooks/subscriptions",
                    headers=headers,
                    json={
                        "callback_url": "http://localhost:9999/webhook",
                        "event_types": ["docling.job.succeeded"],
                        "secret": "0123456789abcdef",
                        "name": "n8n",
                    },
                )
                self.assertEqual(201, subscription.status_code)
                subscription_id = subscription.json()["id"]
                self.assertNotIn("secret", subscription.json())
                self.assertTrue(subscription.json()["secret_set"])
                subscriptions = client.get(
                    "/v1/webhooks/subscriptions", headers=headers
                )
                self.assertEqual(1, subscriptions.json()["count"])
                invalid_secret = "leaked-short"
                invalid_subscription = client.post(
                    "/v1/webhooks/subscriptions",
                    headers=headers,
                    json={
                        "callback_url": "http://localhost:9999/webhook",
                        "event_types": ["docling.job.succeeded"],
                        "secret": invalid_secret,
                    },
                )
                self.assertEqual(422, invalid_subscription.status_code)
                self.assertNotIn(invalid_secret, invalid_subscription.text)
                unsafe_header = client.post(
                    "/v1/webhooks/subscriptions",
                    headers=headers,
                    json={
                        "callback_url": "http://localhost:9999/webhook",
                        "event_types": ["docling.job.succeeded"],
                        "secret": "0123456789abcdef",
                        "headers": {"Host": "internal.invalid"},
                    },
                )
                self.assertEqual(422, unsafe_header.status_code)
                disabled = client.patch(
                    f"/v1/webhooks/subscriptions/{subscription_id}",
                    headers=headers,
                    json={"enabled": False},
                )
                self.assertFalse(disabled.json()["enabled"])
                removed = client.delete(
                    f"/v1/webhooks/subscriptions/{subscription_id}", headers=headers
                )
                self.assertEqual(204, removed.status_code)
                deleted = client.delete(f"/v1/jobs/{job_id}", headers=headers)
                self.assertEqual(204, deleted.status_code)
                after_delete = client.get(f"/v1/jobs/{job_id}", headers=headers)
                self.assertEqual("deleted", after_delete.json()["artifact_state"])
                self.assertIsNotNone(after_delete.json()["deleted_at"])
                self.assertEqual([], list(config.temp_root.glob("docling-upload-*")))


if __name__ == "__main__":
    unittest.main()
