from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import random
import socket
import threading
import unittest
from datetime import datetime, timezone


class _FakeStore:
    def __init__(self, deliveries: list[dict]) -> None:
        self._deliveries = [dict(item) for item in deliveries]
        self.claims: list[tuple] = []
        self.completed: list[dict] = []

    def claim_webhook_delivery(self, now, lease_seconds: int, worker_id: str | None = None):
        self.claims.append((now, lease_seconds, worker_id))
        if not self._deliveries:
            return {}
        return self._deliveries.pop(0)

    def complete_webhook_delivery(self, delivery_id, **kwargs):
        payload = {"delivery_id": delivery_id, **kwargs}
        self.completed.append(payload)
        return payload


def _build_dispatcher(
    store: _FakeStore,
    transport,
    allow_private_hosts: set[str] | None = None,
    resolver=None,
):
    from docling_service.webhook import WebhookDispatcher
    import httpx

    client = httpx.Client(transport=transport, timeout=1.0, follow_redirects=False)
    return WebhookDispatcher(
        store,
        allowed_hosts={"allowed.local", "callback.local", "private.local", "good.local"},
        allow_private_hosts=allow_private_hosts or set(),
        lease_seconds=60,
        poll_interval_seconds=0.0,
        max_attempts_default=3,
        http_timeout_seconds=1.0,
        response_body_limit=64,
        resolver=(
            resolver
            if resolver is not None
            else (lambda host: ["1.1.1.1"] if host == "allowed.local" else ["8.8.8.8"])
        ),
        httpx_client_factory=lambda: client,
    )


def _make_delivery(**overrides: object):
    payload = {
        "id": 12,
        "subscription_id": 7,
        "callback_url": "https://allowed.local/hook",
        "event_type": "job.completed",
        "payload": {"job_id": "job-1"},
        "secret": "topsecret",
        "attempts": 1,
        "max_attempts": 3,
        "job_id": "job-1",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


@unittest.skipIf(
    importlib.util.find_spec("httpx") is None,
    "httpx is not installed in this environment",
)
class WebhookDispatcherTests(unittest.TestCase):
    def test_signature_and_cloudevents_structure(self) -> None:
        import httpx

        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(200, json={"ok": True})

        store = _FakeStore([_make_delivery(attempts=1)])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        result = dispatcher.run_once()
        self.assertTrue(result)
        self.assertEqual(len(requests), 1)
        event = json.loads(requests[0].content.decode("utf-8"))
        self.assertEqual(event["specversion"], "1.0")
        self.assertIn("id", event)
        self.assertEqual(event["type"], "job.completed")
        secret = "topsecret"
        timestamp = requests[0].headers["X-Docling-Signature-Timestamp"]
        expected = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("utf-8") + requests[0].content,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(requests[0].headers["X-Docling-Signature"], expected)
        self.assertEqual("12", requests[0].headers["X-Docling-Delivery-Id"])
        self.assertEqual(len(store.completed), 1)
        self.assertEqual(store.completed[0]["status"], "succeeded")

    def test_subscription_headers_cannot_override_protocol_headers(self) -> None:
        import httpx

        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(204)

        store = _FakeStore([
            _make_delivery(
                headers={
                    "Authorization": "Bearer downstream",
                    "Content-Type": "text/plain",
                    "X-Docling-Signature": "forged",
                    "X-Docling-Event-Id": "forged",
                }
            )
        ])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        self.assertTrue(dispatcher.run_once())
        request = requests[0]
        event = json.loads(request.content)
        self.assertEqual("Bearer downstream", request.headers["Authorization"])
        self.assertEqual("application/cloudevents+json", request.headers["Content-Type"])
        self.assertEqual(event["id"], request.headers["X-Docling-Event-Id"])
        self.assertNotEqual("forged", request.headers["X-Docling-Signature"])

    def test_retryable_status_uses_deadline_with_retry_after(self) -> None:
        import httpx

        original_uniform = random.uniform
        random.uniform = lambda low, high: low

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="throttle")

        before = datetime.now(timezone.utc)
        store = _FakeStore([_make_delivery(attempts=1, max_attempts=3)])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        try:
            self.assertTrue(dispatcher.run_once())
        finally:
            random.uniform = original_uniform

        completion = store.completed[0]
        self.assertEqual(completion["status"], "retrying")
        self.assertEqual(completion["status_code"], 429)
        self.assertIsNotNone(completion["next_attempt_at"])
        next_attempt = datetime.fromisoformat(completion["next_attempt_at"]).astimezone(timezone.utc)
        self.assertGreaterEqual((next_attempt - before).total_seconds(), 1.0)

    def test_non_retryable_4xx_is_dead(self) -> None:
        import httpx

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad")

        store = _FakeStore([_make_delivery(attempts=1, max_attempts=3)])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(store.completed[0]["status"], "failed")
        self.assertEqual(store.completed[0]["status_code"], 400)

    def test_ssrf_private_ip_resolution_is_rejected(self) -> None:
        import httpx

        def handler(_req: httpx.Request) -> httpx.Response:
            self.fail("request should be blocked before dispatch")

        def resolver(host: str) -> list[str]:
            return ["127.0.0.1", "10.0.0.1"] if host == "allowed.local" else ["203.0.113.9"]

        store = _FakeStore([
            _make_delivery(attempts=1, callback_url="https://allowed.local/hook", max_attempts=3)
        ])
        dispatcher = _build_dispatcher(
            store,
            httpx.MockTransport(handler),
            allow_private_hosts={"private.local"},
        )
        dispatcher._resolver = resolver
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(store.completed[0]["status"], "failed")
        self.assertIn("host resolves to non-public address: allowed.local", store.completed[0]["error"])

    def test_redirects_are_not_followed_and_count_as_fail(self) -> None:
        import httpx

        called = threading.Event()

        def handler(req: httpx.Request) -> httpx.Response:
            called.set()
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.local/callback"},
                text="redirect",
            )

        store = _FakeStore([_make_delivery(attempts=1, callback_url="https://allowed.local/hook")])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        self.assertTrue(dispatcher.run_once())
        self.assertTrue(called.is_set())
        self.assertEqual(store.completed[0]["status"], "failed")
        self.assertEqual(store.completed[0]["status_code"], 302)

    def test_event_id_stable_across_delivery_retries(self) -> None:
        import httpx

        requests: list[httpx.Request] = []

        statuses = [500, 200]

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(statuses.pop(0), text="retry" if statuses else "ok")

        # first attempt recorded as attempt 1, second as attempt 2 with same logical delivery
        store = _FakeStore(
            [
                _make_delivery(id=77, attempts=1, max_attempts=3),
                _make_delivery(id=77, attempts=2, max_attempts=3),
            ]
        )
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(store.completed[0]["status"], "retrying")
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(len(requests), 2)
        first = json.loads(requests[0].content.decode("utf-8"))
        second = json.loads(requests[1].content.decode("utf-8"))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(store.completed[1]["status"], "succeeded")

    def test_network_error_is_retried(self) -> None:
        import httpx

        requests: list[httpx.Request] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            requests.append(_req)
            raise httpx.ConnectError("downstream unavailable")

        store = _FakeStore([_make_delivery(attempts=1, max_attempts=3)])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(len(store.completed), 1)
        self.assertEqual(store.completed[0]["status"], "retrying")

    def test_transient_dns_error_is_retried(self) -> None:
        import httpx

        store = _FakeStore([_make_delivery(attempts=1, max_attempts=3)])

        def resolver(_host: str) -> list[str]:
            raise socket.gaierror("temporary resolver failure")

        dispatcher = _build_dispatcher(
            store,
            httpx.MockTransport(
                lambda _request: self.fail("HTTP must not run after DNS failure")
            ),
            resolver=resolver,
        )
        self.assertTrue(dispatcher.run_once())
        self.assertEqual("retrying", store.completed[0]["status"])

    def test_final_allowed_attempt_is_sent_before_failure(self) -> None:
        import httpx

        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(503, text="still unavailable")

        store = _FakeStore([_make_delivery(attempts=3, max_attempts=3)])
        dispatcher = _build_dispatcher(store, httpx.MockTransport(handler))
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(1, len(requests))
        self.assertEqual("failed", store.completed[0]["status"])
