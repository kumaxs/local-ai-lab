from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import hashlib
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from typing import Any

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
                max_webhook_subscriptions=1,
            )

            def runner(command, **_kwargs):
                job_id = command[command.index("--job-id") + 1]
                output_root = Path(command[command.index("--output-root") + 1])
                input_file = Path(command[command.index("--input-file") + 1])
                expected_input_sha256 = None
                if "--expected-input-sha256" in command:
                    marker = command.index("--expected-input-sha256")
                    if marker + 1 < len(command):
                        expected_input_sha256 = command[marker + 1]
                source_pdf_bytes = input_file.read_bytes()
                if expected_input_sha256 is None:
                    expected_input_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
                output_dir = output_root / job_id
                output_dir.mkdir(parents=True)
                (output_dir / "document.html").write_text("<p>Converted</p>", encoding="utf-8")
                (output_dir / "document.md").write_text("# Converted\n", encoding="utf-8")
                (output_dir / "document.json").write_text("{}", encoding="utf-8")
                (output_dir / "metadata.json").write_text(
                    json.dumps(
                        {
                            "original_input_sha256": expected_input_sha256,
                            "visual_evidence_input_sha256": expected_input_sha256,
                            "conversion_input_sha256": expected_input_sha256,
                        }
                    ),
                    encoding="utf-8",
                )
                (output_dir / "status.json").write_text(
                    json.dumps({"ok": True}), encoding="utf-8"
                )
                (output_dir / "source.pdf").write_bytes(source_pdf_bytes)
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
                if os.name != "nt":
                    published = config.output_root / job_id
                    real_published = config.output_root / f"{job_id}.real"
                    published.replace(real_published)
                    published.symlink_to(real_published, target_is_directory=True)
                    try:
                        unsafe_archive = client.get(
                            f"/v1/jobs/{job_id}/archive", headers=headers
                        )
                        self.assertEqual(409, unsafe_archive.status_code)
                    finally:
                        published.unlink()
                        real_published.replace(published)
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
                manager.store.update_job(
                    job_id,
                    output_expires_at="2020-01-01T00:00:00+00:00",
                )
                expired_status = client.get(f"/v1/jobs/{job_id}", headers=headers)
                self.assertEqual("expired", expired_status.json()["artifact_state"])
                expired_outputs = client.get(
                    f"/v1/jobs/{job_id}/outputs", headers=headers
                )
                self.assertEqual(410, expired_outputs.status_code)
                expired_file = client.get(
                    f"/v1/jobs/{job_id}/files/document.md", headers=headers
                )
                self.assertEqual(410, expired_file.status_code)
                expired_archive = client.get(
                    f"/v1/jobs/{job_id}/archive", headers=headers
                )
                self.assertEqual(410, expired_archive.status_code)
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
                subscription_limit = client.post(
                    "/v1/webhooks/subscriptions",
                    headers=headers,
                    json={
                        "callback_url": "http://localhost:9999/second",
                        "event_types": ["docling.job.succeeded"],
                        "secret": "fedcba9876543210",
                    },
                )
                self.assertEqual(429, subscription_limit.status_code)
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
                too_many_events = client.post(
                    "/v1/webhooks/subscriptions",
                    headers=headers,
                    json={
                        "callback_url": "http://localhost:9999/webhook",
                        "event_types": ["docling.job.succeeded"] * 4,
                        "secret": "0123456789abcdef",
                    },
                )
                self.assertEqual(422, too_many_events.status_code)
                oversized_webhook_body = client.post(
                    "/v1/webhooks/subscriptions",
                    headers={**headers, "Content-Type": "application/json"},
                    content=json.dumps({"ignored": "x" * (65 * 1024)}).encode("utf-8"),
                )
                self.assertEqual(413, oversized_webhook_body.status_code)
                self.assertEqual(
                    "request_too_large", oversized_webhook_body.json()["code"]
                )
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

    def test_archive_stream_disconnect_releases_download_lease(self) -> None:
        from docling_service import api
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
                formula_ocr_url="http://localhost:8001",
                webhook_allowed_hosts=("localhost",),
                webhook_allow_private_hosts=True,
            )

            job_id = "00000000-0000-4abc-0000-000000000000"
            output_root = root / "outputs" / job_id
            output_root.mkdir(parents=True)
            content = b"x" * 8192
            (output_root / "document.md").write_bytes(content)
            manifest = [
                {
                    "path": "document.md",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "media_type": "text/markdown",
                }
            ]

            class Lease:
                def __init__(self) -> None:
                    self.renew_count = 0
                    self.release_count = 0
                    self.cancel_count = 0

                def renew(self) -> None:
                    self.renew_count += 1

                def release(self) -> None:
                    self.release_count += 1

                def cancel(self) -> None:
                    self.cancel_count += 1

            lease = Lease()

            class FakeManager:
                def get_job_details(self, requested_job_id: str) -> dict[str, Any]:
                    if requested_job_id != job_id:
                        return {}
                    return {
                        "job_id": job_id,
                        "state": "succeeded",
                        "output_dir": str(output_root),
                        "output_deleted_at": None,
                    }

                def acquire_download_lease(
                    self, requested_job_id: str, _relative_path: str, **_kwargs: Any
                ) -> Lease:
                    if requested_job_id != job_id:
                        raise FileNotFoundError(_relative_path)
                    return lease

                def output_files(self, requested_job_id: str) -> list[dict[str, Any]]:
                    if requested_job_id != job_id:
                        return []
                    return manifest

                def shutdown(self) -> None:
                    return None

            class FakeArchiveIterator:
                def __init__(self) -> None:
                    self.closed = False
                    self.calls = 0

                def __iter__(self) -> "FakeArchiveIterator":
                    return self

                def __next__(self) -> bytes:
                    if self.closed:
                        raise StopIteration
                    self.calls += 1
                    return b"\x80" * 4096

                def close(self) -> None:
                    if self.closed:
                        return
                    self.closed = True
                    lease.cancel()
                    lease.release()

            fake_archive = FakeArchiveIterator()
            original_iter_archive = api.iter_archive
            api.iter_archive = lambda *_args, **_kwargs: fake_archive
            try:
                app = create_app(config=config, manager=FakeManager())

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": f"/v1/jobs/{job_id}/archive",
                    "raw_path": f"/v1/jobs/{job_id}/archive".encode("ascii"),
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"authorization", b"Bearer test-token")],
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 8766),
                }
                sent: list[dict[str, Any]] = []

                async def receive() -> dict[str, Any]:
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message: dict[str, Any]) -> None:
                    sent.append(message)
                    if message["type"] == "http.response.body" and message.get("body"):
                        raise OSError("simulated client disconnect")

                async def invoke() -> None:
                    try:
                        await app(scope, receive, send)
                    except Exception:
                        # Starlette surfaces ClientDisconnect after the response starts.
                        pass

                asyncio.run(invoke())

                self.assertTrue(fake_archive.closed)
                self.assertEqual(1, lease.cancel_count)
                self.assertEqual(1, lease.release_count)
                self.assertGreater(lease.renew_count, 0)
                self.assertTrue(
                    any(
                        message.get("type") == "http.response.start"
                        and message.get("status") == 200
                        for message in sent
                    )
                )
            finally:
                api.iter_archive = original_iter_archive

    def test_request_body_limit_middleware_tracks_concurrency_and_releases_slot(self) -> None:
        from docling_service.api import _RequestBodyLimitMiddleware

        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_app(
                _scope: dict[str, Any], _receive: Any, send: Any
            ) -> None:
                started.set()
                await release.wait()
                await send(
                    {"type": "http.response.start", "status": 202, "headers": []}
                )
                await send(
                    {"type": "http.response.body", "body": b"processing", "more_body": False}
                )

            async def invoke(handler: Any) -> list[dict[str, Any]]:
                response: list[dict[str, Any]] = []

                async def receive() -> dict[str, Any]:
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message: dict[str, Any]) -> None:
                    response.append(message)

                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/jobs",
                    "headers": [],
                }
                await handler(scope, receive, send)
                return response

            middleware = _RequestBodyLimitMiddleware(
                slow_app,
                max_body_bytes=1024 * 1024,
                max_concurrent_uploads=1,
                temp_root=temp_root,
                spool_root=temp_root,
                min_free_bytes=1,
            )

            async def run_concurrency_scenario() -> None:
                first = asyncio.create_task(invoke(middleware))
                await asyncio.sleep(0)
                self.assertTrue(started.is_set())

                blocked = await invoke(middleware)
                self.assertEqual(429, blocked[0]["status"])
                self.assertEqual(
                    "upload_concurrency_exhausted",
                    json.loads(blocked[1]["body"].decode("utf-8"))["code"],
                )

                release.set()
                first_result = await first
                self.assertEqual(202, first_result[0]["status"])
                reused = await invoke(middleware)
                self.assertEqual(202, reused[0]["status"])

            asyncio.run(run_concurrency_scenario())

    def test_request_body_limit_middleware_rejects_when_disk_space_is_low(self) -> None:
        from docling_service.api import _RequestBodyLimitMiddleware

        async def app(scope: dict[str, Any], _receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            middleware = _RequestBodyLimitMiddleware(
                app,
                max_body_bytes=64,
                max_concurrent_uploads=4,
                temp_root=temp_root,
                spool_root=temp_root,
                min_free_bytes=1024,
            )

            async def invoke() -> list[dict[str, Any]]:
                responses: list[dict[str, Any]] = []

                async def receive() -> dict[str, Any]:
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message: dict[str, Any]) -> None:
                    responses.append(message)

                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/jobs",
                    "headers": [],
                }
                await middleware(scope, receive, send)
                return responses

            with patch(
                "docling_service.api.shutil.disk_usage",
                return_value=SimpleNamespace(free=16, total=1024, used=1008),
            ):
                blocked = asyncio.run(invoke())
                self.assertEqual(507, blocked[0]["status"])
                self.assertEqual(
                    "upload_storage_exhausted",
                    json.loads(blocked[1]["body"].decode("utf-8"))["code"],
                )

    def test_request_body_limit_preserves_413_for_chunked_body(self) -> None:
        from docling_service.api import _RequestBodyLimitMiddleware

        async def parser_like_app(_scope: Any, receive: Any, send: Any) -> None:
            try:
                while True:
                    message = await receive()
                    if not message.get("more_body", False):
                        break
            except Exception:
                # Mirrors FastAPI converting a receive failure into a 400.
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"bad request"})

        with tempfile.TemporaryDirectory() as directory:
            middleware = _RequestBodyLimitMiddleware(
                parser_like_app,
                max_body_bytes=8,
                max_concurrent_uploads=1,
                temp_root=Path(directory),
                spool_root=Path(directory),
                min_free_bytes=0,
            )
            chunks = iter(
                [
                    {"type": "http.request", "body": b"12345", "more_body": True},
                    {"type": "http.request", "body": b"67890", "more_body": False},
                ]
            )
            sent: list[dict[str, Any]] = []

            async def receive() -> dict[str, Any]:
                return next(chunks)

            async def send(message: dict[str, Any]) -> None:
                sent.append(message)

            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/jobs",
                "headers": [],
            }
            asyncio.run(middleware(scope, receive, send))
            self.assertEqual(413, sent[0]["status"])
            self.assertEqual(
                "request_too_large",
                json.loads(sent[1]["body"].decode("utf-8"))["code"],
            )

    def test_real_fastapi_chunked_multipart_returns_413_and_releases_slot(self) -> None:
        from fastapi import FastAPI, File

        from docling_service.api import _RequestBodyLimitMiddleware

        inner = FastAPI()

        @inner.post("/v1/jobs")
        async def accept(file: Any = File(...)) -> dict[str, bool]:
            await file.close()
            return {"ok": True}

        boundary = b"docling-boundary"

        def multipart(file_content: bytes) -> bytes:
            return (
                b"--"
                + boundary
                + b'\r\nContent-Disposition: form-data; name="file"; filename="paper.pdf"'
                + b"\r\nContent-Type: application/pdf\r\n\r\n"
                + file_content
                + b"\r\n--"
                + boundary
                + b"--\r\n"
            )

        with tempfile.TemporaryDirectory() as directory:
            middleware = _RequestBodyLimitMiddleware(
                inner,
                max_body_bytes=256,
                max_concurrent_uploads=1,
                temp_root=Path(directory),
                spool_root=Path(directory),
                min_free_bytes=0,
            )

            async def invoke(body: bytes) -> list[dict[str, Any]]:
                chunks = iter(
                    [
                        {"type": "http.request", "body": body[:128], "more_body": True},
                        {"type": "http.request", "body": body[128:], "more_body": False},
                    ]
                )
                sent: list[dict[str, Any]] = []

                async def receive() -> dict[str, Any]:
                    return next(chunks)

                async def send(message: dict[str, Any]) -> None:
                    sent.append(message)

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/v1/jobs",
                    "raw_path": b"/v1/jobs",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [
                        (
                            b"content-type",
                            b"multipart/form-data; boundary=" + boundary,
                        )
                    ],
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 8766),
                }
                await middleware(scope, receive, send)
                return sent

            oversized = asyncio.run(invoke(multipart(b"%PDF-" + b"x" * 300)))
            self.assertEqual(413, oversized[0]["status"])
            accepted = asyncio.run(invoke(multipart(b"%PDF-1.7\n%%EOF\n")))
            self.assertEqual(200, accepted[0]["status"])

    def test_webui_static_assets_have_csp_and_root_is_not_a_redirect(self) -> None:
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
                api_token=None,
                formula_ocr_url="http://formula:8001",
            )

            class FakeManager:
                def shutdown(self) -> None:
                    return None

            with TestClient(create_app(config=config, manager=FakeManager())) as client:
                for path in ("/", "/ui", "/ui/", "/ui/main.js", "/ui/styles.css"):
                    response = client.get(path)
                    self.assertEqual(200, response.status_code, path)
                    self.assertIn("default-src 'self'", response.headers["content-security-policy"])
                self.assertIn("文献处理系统", client.get("/").text)

    def test_system_config_auth_cas_and_strict_patch_contract(self) -> None:
        from fastapi.testclient import TestClient

        from docling_service import api
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
            values = {
                key: {
                    "value": 60,
                    "environment_value": 60,
                    "overridden": False,
                    "minimum": 1,
                    "maximum": 3600,
                    "unit": "seconds",
                    "label": key,
                    "description": "test lifecycle setting",
                    "requires_restart": False,
                }
                for key in (
                    "input_ttl_seconds",
                    "success_output_ttl_seconds",
                    "failed_output_ttl_seconds",
                    "job_ttl_seconds",
                    "staging_ttl_seconds",
                    "temp_ttl_seconds",
                    "cleanup_interval_seconds",
                    "idempotency_ttl_seconds",
                    "download_lease_seconds",
                )
            }

            class FakeManager:
                revision = 4

                def shutdown(self) -> None:
                    return None

                def runtime_config_snapshot(self) -> dict[str, Any]:
                    return {
                        "revision": self.revision,
                        "updated_at": None,
                        "server_time": "2026-08-29T00:00:00+00:00",
                        "existing_job_expiries_unchanged": True,
                        "editable": values,
                        "readonly": {
                            "api_token_configured": {
                                "value": True,
                                "reason": "whether a token is configured",
                            },
                            "max_upload_bytes": {
                                "value": 1024,
                                "reason": "fixed at process start",
                            },
                        },
                    }

                def update_runtime_config(self, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
                    if expected_revision != self.revision:
                        raise api.RuntimeConfigConflict("stale revision")
                    self.revision += 1
                    for key, value in changes.items():
                        values[key]["value"] = values[key]["environment_value"] if value is None else value
                        values[key]["overridden"] = value is not None
                    return self.runtime_config_snapshot()

            manager = FakeManager()
            with TestClient(create_app(config=config, manager=manager)) as client:
                self.assertEqual(401, client.get("/v1/system/config").status_code)
                headers = {"Authorization": "Bearer test-token"}
                snapshot = client.get("/v1/system/config", headers=headers)
                self.assertEqual(200, snapshot.status_code)
                payload = snapshot.json()
                self.assertEqual(9, len(payload["editable"]))
                self.assertNotIn("test-token", snapshot.text)
                self.assertTrue(payload["readonly"]["api_token_configured"]["value"])

                for invalid in (
                    {"revision": 4, "changes": {"max_pending_jobs": 2}},
                    {"revision": 4, "changes": {"input_ttl_seconds": True}},
                    {"revision": 4, "changes": {"input_ttl_seconds": 1.5}},
                    {"revision": 4, "changes": {"input_ttl_seconds": "60"}},
                    {"revision": 4, "changes": {}},
                ):
                    response = client.patch("/v1/system/config", headers=headers, json=invalid)
                    self.assertEqual(422, response.status_code, invalid)

                oversized = client.patch(
                    "/v1/system/config",
                    headers={**headers, "Content-Type": "application/json"},
                    content=(
                        b'{"revision":4,"changes":{"input_ttl_seconds":120},"padding":"'
                        + (b"x" * (70 * 1024))
                        + b'"}'
                    ),
                )
                self.assertEqual(413, oversized.status_code)

                updated = client.patch(
                    "/v1/system/config",
                    headers=headers,
                    json={"revision": 4, "changes": {"input_ttl_seconds": 120}},
                )
                self.assertEqual(200, updated.status_code)
                self.assertEqual(5, updated.json()["revision"])
                conflict = client.patch(
                    "/v1/system/config",
                    headers=headers,
                    json={"revision": 4, "changes": {"input_ttl_seconds": 180}},
                )
                self.assertEqual(409, conflict.status_code)

    def test_job_payload_exposes_progress_and_three_lifecycle_deadlines(self) -> None:
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
                api_token=None,
                formula_ocr_url="http://formula:8001",
            )
            job_id = "00000000-0000-4000-8000-000000000001"

            class FakeManager:
                def shutdown(self) -> None:
                    return None

                def get_job_details(self, requested: str) -> dict[str, Any]:
                    if requested != job_id:
                        return {}
                    return {
                        "job_id": job_id,
                        "state": "running",
                        "original_name": "paper.pdf",
                        "created_at": "2026-08-29T00:00:00+00:00",
                        "input_expires_at": "2026-08-30T00:00:00+00:00",
                        "output_expires_at": "2026-09-01T00:00:00+00:00",
                        "tombstone_expires_at": "2026-09-29T00:00:00+00:00",
                        "progress_stage": "识别正文",
                        "progress_percent": 42,
                        "progress_message": "处理中",
                        "progress_updated_at": "2026-08-29T00:01:00+00:00",
                        "queue_position": 2,
                    }

            with TestClient(create_app(config=config, manager=FakeManager())) as client:
                response = client.get(f"/v1/jobs/{job_id}")
                self.assertEqual(200, response.status_code)
                payload = response.json()
                self.assertEqual("识别正文", payload["progress_stage"])
                self.assertEqual(42, payload["progress_percent"])
                self.assertEqual(2, payload["queue_position"])
                self.assertEqual("2026-08-30T00:00:00+00:00", payload["input_expires_at"])
                self.assertEqual("2026-09-01T00:00:00+00:00", payload["output_expires_at"])
                self.assertEqual("2026-09-29T00:00:00+00:00", payload["tombstone_expires_at"])


if __name__ == "__main__":
    unittest.main()
