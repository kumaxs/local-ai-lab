"""Webhook dispatcher for docling-service v1.1.

The implementation is self-contained and designed for offline testing:
* persistence access is via a tiny protocol (`WebhookStore`)
* HTTP client is injectable
* URL validation and DNS checks are explicit and fail-closed
* delivery lifecycle includes retries and completion calls
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
import hashlib
import hmac
import ipaddress
import inspect
import json
import random
import socket
import threading
import time
import urllib.parse

try:  # pragma: no cover - optional dependency for runtime execution
    import httpx
except ImportError:  # pragma: no cover - tests guard optional dependency
    httpx = None


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    return None


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


class WebhookStore(Protocol):
    def claim_webhook_delivery(self, now: datetime, lease_seconds: int) -> Mapping[str, Any] | dict[str, Any] | None: ...

    def complete_webhook_delivery(self, delivery_id: int | str, **kwargs: Any) -> Any: ...


def _default_resolver(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None)
    addrs: list[str] = []
    for info in infos:
        sock_addr = info[4][0]
        if isinstance(sock_addr, str):
            addrs.append(sock_addr)
    return addrs


def _is_blocked_ip(addresses: list[str]) -> bool:
    for raw in addresses:
        try:
            parsed = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
            return True
    return False


class WebhookResolutionError(ValueError):
    """A callback host could not be resolved due to a transient DNS failure."""


def validate_callback_url(
    callback_url: str,
    *,
    allowed_hosts: set[str] | list[str],
    allow_private_hosts: set[str] | list[str] | None = None,
    resolver: Callable[[str], list[str]] | None = None,
) -> None:
    """Apply the same fail-closed callback policy at create and delivery time."""

    allowed = {str(host).lower() for host in allowed_hosts}
    private = {str(host).lower() for host in (allow_private_hosts or set())}
    parsed = urllib.parse.urlparse(callback_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("scheme must be http or https")
    if parsed.fragment:
        raise ValueError("fragment is not allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("userinfo is not allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("hostname is required")
    if host not in allowed:
        raise ValueError(f"host is not allowed: {host}")
    if parsed.scheme == "http" and host not in private:
        raise ValueError("http callbacks require an explicitly allowed private host")
    try:
        resolved = (resolver or _default_resolver)(host)
    except OSError as exc:
        raise WebhookResolutionError(f"host cannot be resolved: {host}") from exc
    if not resolved:
        raise WebhookResolutionError(f"host cannot be resolved: {host}")
    if host not in private and _is_blocked_ip(resolved):
        raise ValueError(f"host resolves to non-public address: {host}")


@dataclass(frozen=True)
class WebhookDelivery:
    delivery_id: int | str
    callback_url: str
    event_type: str
    payload: Mapping[str, Any]
    attempts: int = 0
    max_attempts: int = 6
    subscription_id: int | str | None = None
    secret: str | None = None
    job_id: str | None = None
    created_at: datetime | None = None
    max_age_seconds: int | None = None
    headers: Mapping[str, Any] | None = None
    event_id: str | None = None

    @classmethod
    def from_record(
        cls,
        record: Mapping[str, Any],
        *,
        fallback_max_attempts: int = 6,
    ) -> "WebhookDelivery":
        payload = record.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            payload = {}

        return cls(
            delivery_id=record["id"] if "id" in record else record["delivery_id"],
            callback_url=str(record["callback_url"]),
            event_type=str(record.get("event_type", "docling.job")),
            payload=dict(payload),
            attempts=_coerce_int(record.get("attempts"), 0),
            max_attempts=_coerce_int(record.get("max_attempts"), fallback_max_attempts),
            subscription_id=record.get("subscription_id"),
            secret=record.get("secret") if isinstance(record.get("secret"), str) else None,
            job_id=str(record["job_id"]) if record.get("job_id") is not None else None,
            created_at=_coerce_datetime(record.get("created_at")),
            max_age_seconds=_coerce_int(record.get("max_age_seconds"), 0) or None,
            headers=record.get("headers") if isinstance(record.get("headers"), Mapping) else None,
            event_id=str(record["event_id"]) if record.get("event_id") is not None else None,
        )


def _call_with_signature(func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(**kwargs)

    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return func(**kwargs)

    filtered = {name: value for name, value in kwargs.items() if name in parameters}
    return func(**filtered)


class WebhookDispatcher:
    """Claim and deliver webhook events."""

    RETRYABLE_EXPLICIT = {408, 425, 429}

    def __init__(
        self,
        store: WebhookStore,
        *,
        allowed_hosts: set[str] | list[str],
        lease_seconds: int = 60,
        poll_interval_seconds: float = 0.5,
        max_attempts_default: int = 6,
        max_age_seconds: int = 7 * 24 * 60 * 60,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        response_body_limit: int = 32 * 1024,
        allow_private_hosts: set[str] | list[str] | None = None,
        http_timeout_seconds: float = 5.0,
        resolver: Callable[[str], list[str]] | None = None,
        httpx_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        self._allowed_hosts = {str(host).lower() for host in allowed_hosts}
        if not self._allowed_hosts:
            raise ValueError("allowed_hosts is required under fail-closed policy")
        self._allow_private_hosts = {str(host).lower() for host in (allow_private_hosts or set())}
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._max_attempts_default = max_attempts_default
        self._max_age_seconds = max_age_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._response_body_limit = response_body_limit
        self._resolver = resolver or _default_resolver
        self._worker_id = f"docling-webhook-{threading.get_ident()}"

        if httpx_client_factory is None:
            if httpx is None:
                raise RuntimeError("httpx is required for webhook dispatching")
            self._httpx_client_factory = lambda: httpx.Client(
                timeout=http_timeout_seconds,
                follow_redirects=False,
            )
        else:
            self._httpx_client_factory = httpx_client_factory
        self._http_client = self._httpx_client_factory()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _invoke_claim(self, now: datetime) -> Mapping[str, Any] | None:
        kwargs = {"now": now, "lease_seconds": self._lease_seconds}
        try:
            signature = inspect.signature(self._store.claim_webhook_delivery)
            if "worker_id" in signature.parameters:
                kwargs["worker_id"] = self._worker_id
        except (TypeError, ValueError):
            kwargs = {"now": now, "lease_seconds": self._lease_seconds}
        result = self._store.claim_webhook_delivery(**kwargs)  # type: ignore[arg-type]
        if isinstance(result, Mapping) and result:
            return result
        return None

    def _complete_webhook(self, delivery_id: int | str, *, status: str, success: bool, **extra: Any) -> Any:
        payload = {
            "success": success,
            "status": status,
            "status_code": extra.get("status_code"),
            "response": extra.get("response"),
            "error": extra.get("error"),
            "next_attempt_at": extra.get("next_attempt_at"),
            "attempts": extra.get("attempts"),
            "worker_id": self._worker_id,
        }
        if isinstance(payload["next_attempt_at"], datetime):
            payload["next_attempt_at"] = payload["next_attempt_at"].isoformat()
        # Keep compatibility with stricter signatures by filtering unknown args.
        payload = {key: value for key, value in payload.items() if value is not None}
        return _call_with_signature(
            self._store.complete_webhook_delivery,
            {"delivery_id": delivery_id, **payload},
        )

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _parse_retry_after(self, value: str | None, now: datetime) -> timedelta | None:
        if not value:
            return None
        try:
            return timedelta(seconds=max(1, int(value.strip())))
        except ValueError:
            pass
        try:
            parsed = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT")
            parsed = parsed.replace(tzinfo=timezone.utc)
            delta = parsed - now
            if delta.total_seconds() <= 0:
                return timedelta(seconds=1)
            return delta
        except Exception:
            return None

    def _retry_delay(self, attempts: int, retry_after: str | None) -> float:
        # attempts is current attempt count, e.g. 1 for first attempt.
        base = self._retry_base_seconds * (2 ** max(0, attempts - 1))
        base = min(base, self._retry_max_seconds)
        jitter = base * 0.2
        delay = random.uniform(max(0.0, base - jitter), base + jitter)
        retry_after_delay = self._parse_retry_after(retry_after, self._now())
        if retry_after_delay is None:
            return delay
        return max(delay, retry_after_delay.total_seconds())

    def _stable_event_id(self, delivery: WebhookDelivery, payload: bytes) -> str:
        if delivery.event_id:
            return delivery.event_id
        seed = {
            "id": str(delivery.delivery_id),
            "subscription_id": str(delivery.subscription_id) if delivery.subscription_id is not None else "",
            "type": delivery.event_type,
            "job_id": delivery.job_id or "",
            "payload": delivery.payload,
            "payload_hash": hashlib.sha256(payload).hexdigest(),
        }
        raw = json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _build_cloudevent(self, delivery: WebhookDelivery) -> tuple[str, dict[str, Any], bytes]:
        event_time = self._now().isoformat()
        body_obj = {
            "specversion": "1.0",
            "source": "urn:docling-service:webhook",
            "type": delivery.event_type,
            "time": event_time,
            "id": "",
            "datacontenttype": "application/json",
            "data": dict(delivery.payload),
        }
        if delivery.job_id is not None:
            body_obj["subject"] = delivery.job_id
        raw_json = json.dumps(
            {
                "type": delivery.event_type,
                "job_id": delivery.job_id,
                "subscription_id": (
                    str(delivery.subscription_id) if delivery.subscription_id is not None else ""
                ),
                "payload": dict(delivery.payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        # stable id must be computed from canonical payload, not event time.
        body_obj["id"] = self._stable_event_id(delivery, raw_json)
        body = json.dumps(body_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return body_obj["id"], body_obj, body

    def _signature(self, secret: str, timestamp: str, body: bytes) -> str:
        digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()
        return digest

    def _event_headers(self, delivery: WebhookDelivery, event: Mapping[str, Any], body: bytes) -> dict[str, str]:
        event_time = str(event.get("time", ""))
        # Use Unix seconds to keep deterministic signing inputs.
        timestamp = str(int(datetime.fromisoformat(event_time.replace("Z", "+00:00")).timestamp()))
        headers: dict[str, str] = {
            str(key): value
            for key, value in (delivery.headers or {}).items()
            if isinstance(value, str)
        }
        # Protocol headers always win over subscription-provided headers. This
        # prevents a caller from replacing the body signature or event identity.
        headers.update({
            "Content-Type": "application/cloudevents+json",
            "X-Docling-Event-Id": str(event.get("id")),
            "X-Docling-Event-Type": str(event.get("type")),
            "X-Docling-Event-Time": event_time,
            "X-Docling-Delivery-Id": str(delivery.delivery_id),
            "X-Docling-Signature-Timestamp": timestamp,
        })
        if delivery.secret:
            headers["X-Docling-Signature"] = self._signature(delivery.secret, timestamp, body)
        return headers

    def _validate_url(self, callback_url: str) -> None:
        validate_callback_url(
            callback_url,
            allowed_hosts=self._allowed_hosts,
            allow_private_hosts=self._allow_private_hosts,
            resolver=self._resolver,
        )

    def _read_response_body(self, response: Any) -> str:
        chunks = []
        total = 0
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            remain = self._response_body_limit - total
            if remain <= 0:
                break
            piece = chunk[:remain]
            chunks.append(piece)
            total += len(piece)
            if total >= self._response_body_limit:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _run_delivery(self, delivery: WebhookDelivery) -> None:
        now = self._now()
        try:
            self._validate_url(delivery.callback_url)
        except WebhookResolutionError as exc:
            self._handle_failure(
                delivery,
                status_code=None,
                response_body=None,
                error=str(exc),
                network_error=True,
            )
            return
        except ValueError as exc:
            self._complete_webhook(
                delivery.delivery_id,
                status="failed",
                success=False,
                error=str(exc),
                attempts=delivery.attempts,
            )
            return

        max_age_seconds = (
            delivery.max_age_seconds
            if delivery.max_age_seconds is not None
            else self._max_age_seconds
        )
        if delivery.created_at is not None and max_age_seconds > 0:
            if (now - delivery.created_at).total_seconds() > max_age_seconds:
                self._complete_webhook(
                    delivery.delivery_id,
                    status="failed",
                    success=False,
                    status_code=None,
                    error="delivery expired",
                    attempts=delivery.attempts,
                )
                return

        _, event, body = self._build_cloudevent(delivery)
        headers = self._event_headers(delivery, event, body)

        try:
            with self._http_client.stream(
                "POST",
                delivery.callback_url,
                headers=headers,
                content=body,
                follow_redirects=False,
            ) as response:
                response_body = self._read_response_body(response)
                status_code = int(response.status_code)
                retry_after = response.headers.get("retry-after")
        except Exception as exc:  # pragma: no cover - exercised via synthetic failures
            self._handle_failure(
                delivery,
                status_code=None,
                response_body=None,
                error=str(exc),
                network_error=True,
            )
            return

        self._handle_response(
            delivery,
            status_code=status_code,
            response_body=response_body,
            retry_after=retry_after,
        )

    def _handle_response(
        self,
        delivery: WebhookDelivery,
        *,
        status_code: int,
        response_body: str | None,
        retry_after: str | None = None,
    ) -> None:
        if 200 <= status_code < 300:
            self._complete_webhook(
                delivery.delivery_id,
                status="succeeded",
                success=True,
                status_code=status_code,
                response=response_body,
                attempts=delivery.attempts,
            )
            return

        self._handle_failure(
            delivery,
            status_code=status_code,
            response_body=response_body,
            error=f"HTTP {status_code}",
            retry_after=retry_after,
        )

    def _handle_failure(
        self,
        delivery: WebhookDelivery,
        *,
        status_code: int | None,
        response_body: str | None,
        error: str,
        retry_after: str | None = None,
        network_error: bool = False,
    ) -> None:
        max_attempts = delivery.max_attempts or self._max_attempts_default
        retryable = network_error or (
            status_code is not None
            and (
                status_code in self.RETRYABLE_EXPLICIT
                or status_code >= 500
            )
        )
        if retryable and delivery.attempts < max_attempts:
            next_attempt = self._now() + timedelta(
                seconds=self._retry_delay(delivery.attempts, retry_after)
            )
            self._complete_webhook(
                delivery.delivery_id,
                status="retrying",
                success=False,
                status_code=status_code,
                response=response_body,
                error=error,
                next_attempt_at=next_attempt,
                attempts=delivery.attempts,
            )
            return

        self._complete_webhook(
            delivery.delivery_id,
            status="failed",
            success=False,
            status_code=status_code,
            response=response_body,
            error=error,
            attempts=delivery.attempts,
        )

    def run_once(self) -> bool:
        now = self._now()
        record = self._invoke_claim(now)
        if not record:
            return False

        try:
            delivery = WebhookDelivery.from_record(
                record,
                fallback_max_attempts=self._max_attempts_default,
            )
        except Exception as exc:  # pragma: no cover - defensive path
            delivery_id = record.get("id", "unknown")
            self._complete_webhook(
                delivery_id,
                status="failed",
                success=False,
                error=f"invalid record: {exc}",
            )
            return True

        # Claiming increments attempts. Equality is the final permitted network
        # attempt; only an over-limit record is rejected without dispatch.
        if delivery.attempts > delivery.max_attempts:
            self._complete_webhook(
                delivery.delivery_id,
                status="failed",
                success=False,
                status_code=None,
                error="max attempts reached",
                attempts=delivery.attempts,
            )
            return True

        with self._lock:
            try:
                self._run_delivery(delivery)
            except Exception as exc:
                self._complete_webhook(
                    delivery.delivery_id,
                    status="failed",
                    success=False,
                    error=f"delivery dispatch error: {exc}",
                    attempts=delivery.attempts,
                )
        return True

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            handled = self.run_once()
            if not handled:
                self._stop_event.wait(self._poll_interval_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, *, wait: float | None = 1.0) -> None:
        self._stop_event.set()
        if self._thread is None:
            return
        self._thread.join(timeout=wait)

    def drain(self, timeout: float = 10.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("drain requires dispatcher to be stopped")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.run_once():
                break

    def close(self) -> None:
        self.stop()
        if hasattr(self._http_client, "close"):
            self._http_client.close()
