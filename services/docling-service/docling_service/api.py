"""Versioned HTTP API for the formal docling-service releases."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import release as release_module

from .api_models import (
    CapabilitiesResponse,
    HealthResponse,
    JobCloudEvent,
    JobCreateResponse,
    JobLinks,
    JobListResponse,
    JobResponse,
    ManifestResponse,
    OutputListResponse,
    ProblemDetails,
    RUNTIME_CONFIG_KEYS,
    SystemConfigPatchRequest,
    SystemConfigResponse,
    StorageResponse,
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookRetryResponse,
    WebhookSubscriptionCreate,
    WebhookSubscriptionListResponse,
    WebhookSubscriptionResponse,
    WebhookSubscriptionUpdate,
)
from .archive import ArchiveError, iter_archive, preflight_archive
from .lifecycle import QueueFullError, StorageQuotaError

from .release import (
    RELEASE_VERSION,
    JobManager,
    OutputExpiredError,
    ReleaseConfig,
    probe_backend,
    probe_formula_service,
    utc_now,
)


RuntimeConfigConflict = getattr(release_module, "RuntimeConfigConflict", None)


if RuntimeConfigConflict is None:  # pragma: no cover - compatibility path
    class RuntimeConfigConflict(RuntimeError):
        """Fallback when release runtime config CAS exceptions are unavailable."""


LOGGER = logging.getLogger(__name__)


class _RequestBodyTooLarge(OSError):
    """OSError subclass so Starlette closes multipart spool files on abort."""

    pass


class _RequestBodyLimitMiddleware:
    """Bound multipart request bytes before Starlette spools an upload."""

    def __init__(
        self,
        app: Any,
        *,
        max_body_bytes: int,
        max_concurrent_uploads: int,
        temp_root: Path,
        spool_root: Path,
        min_free_bytes: int,
        max_webhook_body_bytes: int = 64 * 1024,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_concurrent_uploads = max_concurrent_uploads
        self.temp_root = temp_root
        self.spool_root = spool_root
        self.min_free_bytes = min_free_bytes
        self.max_webhook_body_bytes = max_webhook_body_bytes
        self._reservation_bytes = max_body_bytes * 2
        self._active = 0
        self._reserved = 0
        self._lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        method = scope.get("method")
        path = scope.get("path", "")
        is_job_upload = method == "POST" and path == "/v1/jobs"
        is_webhook_write = (
            (method == "POST" and path == "/v1/webhooks/subscriptions")
            or (
                method == "PATCH"
                and path.startswith("/v1/webhooks/subscriptions/")
            )
        )
        is_config_write = method == "PATCH" and path == "/v1/system/config"
        if scope.get("type") != "http" or not (
            is_job_upload or is_webhook_write or is_config_write
        ):
            await self.app(scope, receive, send)
            return

        body_limit = (
            self.max_body_bytes if is_job_upload else self.max_webhook_body_bytes
        )

        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == b"content-length":
                try:
                    if int(raw_value) > body_limit:
                        await self._reject(scope, send, status=413, code="request_too_large")
                        return
                except ValueError:
                    pass

        admitted = False
        if is_job_upload:
            with self._lock:
                free = shutil.disk_usage(self.temp_root).free
                spool_free = shutil.disk_usage(self.spool_root).free
                if self._active >= self.max_concurrent_uploads:
                    rejection = (429, "upload_concurrency_exhausted")
                elif free - self._reserved - self._reservation_bytes < self.min_free_bytes:
                    rejection = (507, "upload_storage_exhausted")
                elif (
                    self.spool_root.resolve() != self.temp_root.resolve()
                    and spool_free - self._reserved - self._reservation_bytes
                    < self.min_free_bytes
                ):
                    rejection = (507, "upload_storage_exhausted")
                else:
                    rejection = None
                    admitted = True
                    self._active += 1
                    self._reserved += self._reservation_bytes
            if rejection is not None:
                await self._reject(scope, send, status=rejection[0], code=rejection[1])
                return

        received = 0
        oversized = False
        response_started = False

        async def bounded_receive() -> Any:
            nonlocal oversized, received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    oversized = True
                    raise _RequestBodyTooLarge
            return message

        async def bounded_send(message: Any) -> None:
            nonlocal response_started
            # FastAPI may translate a receive exception into a 400. Suppress
            # that response so this outer middleware can preserve the 413
            # contract for chunked/no-Content-Length requests.
            if oversized:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            try:
                await self.app(scope, bounded_receive, bounded_send)
            except _RequestBodyTooLarge:
                pass
            if oversized and not response_started:
                await self._reject(scope, send, status=413, code="request_too_large")
        finally:
            if admitted:
                with self._lock:
                    self._active -= 1
                    self._reserved -= self._reservation_bytes

    @staticmethod
    async def _reject(scope: Any, send: Any, *, status: int, code: str) -> None:
        descriptions = {
            "request_too_large": (
                "Request body is too large",
                "request body exceeds the configured endpoint limit",
            ),
            "upload_concurrency_exhausted": (
                "Too many uploads",
                "the concurrent upload admission limit is exhausted",
            ),
            "upload_storage_exhausted": (
                "Upload storage is exhausted",
                "the temporary filesystem cannot preserve the configured free-space reserve",
            ),
        }
        title, detail = descriptions[code]
        payload = json.dumps(
            {
                "type": f"urn:docling:error:{code}",
                "title": title,
                "status": status,
                "detail": detail,
                "instance": scope.get("path", "/v1/jobs"),
                "code": code,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def create_app(config: ReleaseConfig | None = None, manager: JobManager | None = None) -> Any:
    try:
        from fastapi import (
            Depends,
            FastAPI,
            File,
            Form,
            Header,
            HTTPException,
            Query,
            Request,
            Response,
            UploadFile,
            status,
        )
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import (
            FileResponse,
            JSONResponse,
            StreamingResponse,
        )
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
        from starlette.exceptions import HTTPException as StarletteHTTPException
        from starlette.requests import ClientDisconnect
    except ImportError as exc:  # pragma: no cover - exercised by packaging smoke tests
        raise RuntimeError(
            "HTTP dependencies are missing; install docling-service[api]"
        ) from exc

    actual_config = config or ReleaseConfig.from_env()
    actual_manager = manager or JobManager(actual_config)

    class CloseableStreamingResponse(StreamingResponse):
        """Close a synchronous producer even when the ASGI client disconnects."""

        def __init__(self, *args: Any, close: Any = None, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._close_stream = close

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            try:
                await super().__call__(scope, receive, send)
            except ClientDisconnect:
                # A client abandoning a download is expected transport behavior.
                return
            finally:
                if self._close_stream is not None:
                    try:
                        self._close_stream()
                    except Exception:
                        LOGGER.exception("failed to close streaming response producer")

    @asynccontextmanager
    async def lifespan(_app: Any):
        yield
        actual_manager.shutdown()

    ui_assets_root = Path(__file__).resolve().parent / "ui"

    def _ui_security_headers(response: Response) -> None:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"

    def _serve_ui_file(relative_path: str, default_filename: str | None = None) -> Any:
        path_obj = Path(relative_path)
        if (
            path_obj.is_absolute()
            or any(part == ".." for part in path_obj.parts)
            or path_obj.name in {"", "."}
        ):
            raise HTTPException(status_code=404, detail="ui asset not found")
        if default_filename and relative_path == "":
            path_obj = Path(default_filename)
        target = (ui_assets_root / path_obj).resolve()
        ui_root_resolved = ui_assets_root.resolve()
        if not str(target).startswith(str(ui_root_resolved) + os.sep) and target != ui_root_resolved:
            raise HTTPException(status_code=404, detail="ui asset not found")
        if not target.exists() or target.is_dir():
            raise HTTPException(status_code=404, detail="ui asset not found")
        media_type, _ = mimetypes.guess_type(target.name)
        if media_type is None:
            media_type = "application/octet-stream"
        response = FileResponse(target, media_type=media_type)
        _ui_security_headers(response)
        return response

    problem_documentation = {
        code: {
            "description": "RFC 9457 Problem Details",
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"}
                }
            },
        }
        for code in (400, 401, 404, 409, 410, 413, 415, 422, 429, 500, 503, 507)
    }
    app = FastAPI(
        title="Local AI Lab Docling Service",
        version=RELEASE_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_version="3.1.0",
        lifespan=lifespan,
        responses=problem_documentation,
    )
    # Multipart framing is allowed a fixed 1 MiB overhead, but the complete
    # request remains bounded before UploadFile can spill arbitrary bytes.
    app.add_middleware(
        _RequestBodyLimitMiddleware,
        max_body_bytes=actual_config.max_upload_bytes + 1024 * 1024,
        max_concurrent_uploads=actual_config.max_concurrent_uploads,
        temp_root=actual_config.temp_root,
        spool_root=Path(tempfile.gettempdir()),
        min_free_bytes=actual_config.min_free_bytes,
    )

    bearer = HTTPBearer(auto_error=False)

    def problem_response(
        request: Any,
        *,
        status_code: int,
        title: str,
        detail: str,
        code: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        payload = ProblemDetails(
            type=f"urn:docling:error:{code}",
            title=title,
            status=status_code,
            detail=detail,
            instance=str(request.url.path),
            code=code,
        )
        return JSONResponse(
            status_code=status_code,
            content=payload.model_dump(exclude_none=True),
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Any, exc: Any) -> Any:
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return problem_response(
            request,
            status_code=exc.status_code,
            title="HTTP request failed",
            detail=detail,
            code=f"http_{exc.status_code}",
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Any, exc: RequestValidationError
    ) -> Any:
        issues = []
        for issue in exc.errors():
            location = ".".join(str(part) for part in issue.get("loc", ()))
            issues.append(f"{location}: {issue.get('msg', 'invalid value')}")
        return problem_response(
            request,
            status_code=422,
            title="Request validation failed",
            detail="; ".join(issues) or "request validation failed",
            code="validation_error",
        )

    @app.exception_handler(Exception)
    async def unexpected_exception_handler(request: Any, exc: Exception) -> Any:
        LOGGER.exception("unhandled API error", exc_info=exc)
        return problem_response(
            request,
            status_code=500,
            title="Internal server error",
            detail="the request could not be completed",
            code="internal_error",
        )

    def authorize(
        credentials: Any = Depends(bearer),
    ) -> None:
        token = actual_config.api_token
        if token is None:
            return
        if (
            credentials is None
            or credentials.scheme.casefold() != "bearer"
            or not hmac.compare_digest(credentials.credentials, token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    _secret_readonly_markers = (
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "api_key",
    )

    def _safe_system_config_snapshot(snapshot: Any) -> dict[str, Any]:
        """Validate and redact a manager snapshot before it crosses HTTP.

        The release manager owns the canonical shape.  This small boundary
        adapter keeps older managers useful during rolling upgrades while
        ensuring that an accidental environment dump (especially an API token)
        can never be reflected by the web UI.
        """

        if isinstance(snapshot, BaseException):
            raise HTTPException(status_code=500, detail="invalid runtime configuration snapshot")
        if hasattr(snapshot, "model_dump"):
            snapshot = snapshot.model_dump()
        if not isinstance(snapshot, Mapping):
            raise HTTPException(
                status_code=500,
                detail="invalid runtime configuration snapshot",
            )

        # The final manager contract uses ``editable`` entries.  A short-lived
        # compatibility path accepts the pre-CAS runtime/environment mapping
        # and converts it to the same safe wire shape.  It is intentionally
        # limited to the nine lifecycle values.
        editable_raw = snapshot.get("editable")
        if not isinstance(editable_raw, Mapping):
            runtime = snapshot.get("runtime")
            environment = snapshot.get("environment")
            if isinstance(runtime, Mapping) and isinstance(environment, Mapping):
                converted: dict[str, Any] = {}
                for key in RUNTIME_CONFIG_KEYS:
                    if key not in runtime:
                        continue
                    value = runtime[key]
                    env_value = environment.get(key, value)
                    converted[key] = {
                        "value": value,
                        "environment_value": env_value,
                        "overridden": value != env_value,
                        "minimum": 1,
                        "maximum": 2**63 - 1,
                        "unit": "seconds",
                        "label": key,
                        "description": "runtime lifecycle setting",
                        "requires_restart": False,
                    }
                editable_raw = converted
            else:
                editable_raw = {}

        editable: dict[str, Any] = {}
        if isinstance(editable_raw, Mapping):
            for key in RUNTIME_CONFIG_KEYS:
                entry = editable_raw.get(key)
                if not isinstance(entry, Mapping):
                    continue
                # Keep only the documented entry fields.  Values are checked
                # strictly by SystemConfigEditableValue below.
                editable[key] = {
                    "value": entry.get("value"),
                    "environment_value": entry.get("environment_value"),
                    "overridden": entry.get("overridden", False),
                    "minimum": entry.get("minimum", 1),
                    "maximum": entry.get("maximum", 2**63 - 1),
                    "unit": entry.get("unit", "seconds"),
                    "label": entry.get("label", key),
                    "description": entry.get("description", "runtime lifecycle setting"),
                    "requires_restart": entry.get("requires_restart", False),
                }

        readonly_raw = snapshot.get("readonly")
        readonly: dict[str, Any] = {}
        if isinstance(readonly_raw, Mapping):
            for raw_key, raw_entry in readonly_raw.items():
                key = str(raw_key)
                lowered = key.casefold()
                # A secret-bearing setting may only cross the boundary as an
                # explicit configured boolean.  Never preserve its raw value.
                if any(marker in lowered for marker in _secret_readonly_markers):
                    configured_key = lowered == "api_token" or lowered.endswith(
                        ("_configured", "_set", "configured", "set")
                    )
                    if not configured_key:
                        continue
                    if isinstance(raw_entry, Mapping):
                        candidate = raw_entry.get("value")
                        if not isinstance(candidate, bool):
                            candidate = bool(candidate)
                        raw_entry = {
                            "value": candidate,
                            "reason": raw_entry.get("reason") or "whether a credential is configured",
                        }
                    elif isinstance(raw_entry, bool):
                        raw_entry = {
                            "value": raw_entry,
                            "reason": "whether a credential is configured",
                        }
                    else:
                        continue
                if isinstance(raw_entry, Mapping):
                    readonly[key] = {
                        "value": raw_entry.get("value"),
                        "reason": raw_entry.get("reason") or "read-only service setting",
                    }

        return {
            "revision": snapshot.get("revision", 0),
            "updated_at": snapshot.get("updated_at"),
            "server_time": snapshot.get("server_time") or utc_now(),
            "existing_job_expiries_unchanged": bool(
                snapshot.get("existing_job_expiries_unchanged", True)
            ),
            "editable": editable,
            "readonly": readonly,
        }

    def _system_config_response(snapshot: Any) -> SystemConfigResponse:
        try:
            return SystemConfigResponse(**_safe_system_config_snapshot(snapshot))
        except HTTPException:
            raise
        except Exception as exc:
            LOGGER.warning("invalid runtime configuration snapshot: %s", exc)
            raise HTTPException(
                status_code=500,
                detail="invalid runtime configuration snapshot",
            ) from None

    @app.get("/", include_in_schema=False)
    def ui_root() -> Any:
        response = _serve_ui_file("index.html")
        _ui_security_headers(response)
        return response

    @app.get("/ui", include_in_schema=False)
    def ui_root_without_slash() -> Any:
        response = _serve_ui_file("index.html")
        _ui_security_headers(response)
        return response

    @app.get("/ui/", include_in_schema=False)
    def ui_index() -> Any:
        response = _serve_ui_file("index.html")
        _ui_security_headers(response)
        return response

    @app.get("/ui/{asset_path:path}", include_in_schema=False)
    def ui_asset(asset_path: str) -> Any:
        if asset_path == "":
            response = _serve_ui_file("index.html")
        else:
            response = _serve_ui_file(asset_path)
        _ui_security_headers(response)
        return response

    def job_payload(record: Mapping[str, Any]) -> JobResponse:
        job_id = str(record["job_id"])
        output_deadline = record.get("output_expires_at")
        output_deadline_passed = False
        if isinstance(output_deadline, str) and output_deadline:
            try:
                normalized_deadline = output_deadline.replace("Z", "+00:00")
                deadline = datetime.fromisoformat(normalized_deadline)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                output_deadline_passed = deadline <= datetime.now(timezone.utc)
            except ValueError:
                output_deadline_passed = False
        if record.get("output_deleted_at"):
            artifact_state = "deleted"
        elif record.get("state") in {"queued", "running"}:
            artifact_state = "pending"
        elif output_deadline_passed:
            artifact_state = "expired"
        elif Path(str(record.get("output_dir", ""))).is_dir():
            artifact_state = "available"
        else:
            artifact_state = "expired"

        # Progress is optional in persisted records and may be represented by
        # either flat columns or a nested ``progress`` object while workers are
        # upgraded.  Normalize both forms to the stable HTTP contract.
        progress = record.get("progress")
        progress_map = progress if isinstance(progress, Mapping) else {}
        progress_stage = record.get("progress_stage") or progress_map.get("stage")
        progress_percent = record.get("progress_percent")
        if progress_percent is None:
            progress_percent = progress_map.get("percent")
        progress_message = record.get("progress_message") or progress_map.get("message")
        progress_updated_at = record.get("progress_updated_at") or progress_map.get(
            "updated_at"
        )
        queue_position = record.get("queue_position")
        if queue_position is None:
            queue_position = record.get("queue")
            if isinstance(queue_position, Mapping):
                queue_position = queue_position.get("position")
        known = {
            "job_id": job_id,
            "state": record.get("state", "queued"),
            "original_name": record.get("original_name") or "document.pdf",
            "client_reference": record.get("client_reference"),
            "created_at": record.get("created_at") or utc_now(),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "exit_code": record.get("exit_code"),
            "error": record.get("error"),
            "input_size_bytes": record.get("input_size_bytes", record.get("input_bytes", 0)) or 0,
            "output_size_bytes": record.get("output_size_bytes", record.get("output_bytes", 0)) or 0,
            "input_expires_at": record.get("input_expires_at"),
            "output_expires_at": record.get("output_expires_at"),
            "tombstone_expires_at": record.get("tombstone_expires_at"),
            "input_deleted_at": record.get("input_deleted_at"),
            "output_deleted_at": record.get("output_deleted_at"),
            "deleted_at": record.get("deleted_at"),
            "progress_stage": progress_stage,
            "progress_percent": progress_percent,
            "progress_message": progress_message,
            "progress_updated_at": progress_updated_at,
            "queue_position": queue_position,
        }
        return JobResponse(
            **known,
            artifact_state=artifact_state,
            server_time=utc_now(),
            outputs_url=f"/v1/jobs/{job_id}/outputs",
            links=JobLinks(
                status=f"/v1/jobs/{job_id}",
                outputs=f"/v1/jobs/{job_id}/outputs",
                manifest=f"/v1/jobs/{job_id}/manifest",
                archive=f"/v1/jobs/{job_id}/archive",
            ),
        )

    def subscription_payload(record: dict[str, Any]) -> WebhookSubscriptionResponse:
        return WebhookSubscriptionResponse(
            **{key: value for key, value in record.items() if key != "secret"},
            secret_set=bool(record.get("secret")),
        )

    def file_chunks(handle: Any, lease: Any):
        try:
            with handle:
                while chunk := handle.read(1024 * 1024):
                    lease.renew()
                    yield chunk
        finally:
            lease.release()

    @app.get("/healthz", tags=["system"], response_model=HealthResponse)
    def healthz() -> HealthResponse:
        backend = probe_backend(actual_config)
        formula = probe_formula_service(actual_config)
        payload = {
            "ok": bool(backend["ok"] and formula["ok"]),
            "service": "docling-service",
            "profile": actual_config.profile,
            "backend": backend,
            "formula": formula,
        }
        if not payload["ok"]:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="conversion dependencies are not ready",
            )
        return HealthResponse(**payload)

    @app.get(
        "/v1/capabilities",
        tags=["system"],
        dependencies=[Depends(authorize)],
        response_model=CapabilitiesResponse,
    )
    def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse(**actual_config.public_capabilities())

    @app.post(
        "/v1/jobs",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["jobs"],
        dependencies=[Depends(authorize)],
        response_model=JobCreateResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {
                                "file": {"type": "string", "format": "binary"},
                                "client_reference": {
                                    "type": ["string", "null"],
                                    "maxLength": 200,
                                },
                            },
                        }
                    }
                },
            }
        },
    )
    async def create_job(
        file: Any = File(...),
        client_reference: str | None = Form(default=None),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=r".*\S.*",
        ),
    ) -> JobCreateResponse:
        original_name = Path(file.filename or "document.pdf").name
        if not original_name.casefold().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="only PDF uploads are supported")
        if client_reference is not None and len(client_reference) > 200:
            raise HTTPException(status_code=422, detail="client_reference is too long")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="docling-upload-", suffix=".pdf", dir=actual_config.temp_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        total = 0
        first = b""
        protected = False
        try:
            actual_manager.protect_temp_file(temporary)
            protected = True
            with temporary.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    if not first:
                        first = chunk[:5]
                    total += len(chunk)
                    if total > actual_config.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="PDF exceeds upload limit")
                    output.write(chunk)
            if total == 0 or not first.startswith(b"%PDF-"):
                raise HTTPException(status_code=415, detail="upload is not a PDF file")
            try:
                record, replayed = actual_manager.submit_job(
                    temporary,
                    original_name,
                    idempotency_key=idempotency_key,
                    client_reference=client_reference,
                )
            except FileExistsError:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used for different input",
                ) from None
            except QueueFullError:
                raise HTTPException(status_code=429, detail="pending job queue is full") from None
            except StorageQuotaError:
                raise HTTPException(status_code=507, detail="storage quota is exhausted") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                try:
                    if protected:
                        actual_manager.release_temp_file(temporary)
                finally:
                    await file.close()
        return JobCreateResponse(
            job_id=record.job_id,
            state=record.state,
            status_url=f"/v1/jobs/{record.job_id}",
            outputs_url=f"/v1/jobs/{record.job_id}/outputs",
            manifest_url=f"/v1/jobs/{record.job_id}/manifest",
            archive_url=f"/v1/jobs/{record.job_id}/archive",
            idempotent_replay=replayed,
        )

    @app.get(
        "/v1/jobs",
        tags=["jobs"],
        dependencies=[Depends(authorize)],
        response_model=JobListResponse,
    )
    def list_jobs(
        state_filter: str | None = Query(default=None, alias="state"),
        client_reference: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> JobListResponse:
        if state_filter is not None and state_filter not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "interrupted",
        }:
            raise HTTPException(status_code=422, detail="invalid job state")
        page = actual_manager.list_jobs(
            state=state_filter,
            client_reference=client_reference,
            cursor=cursor,
            limit=limit,
        )
        if page.get("error"):
            raise HTTPException(status_code=400, detail=str(page["error"]))
        return JobListResponse(
            items=[job_payload(item) for item in page.get("items", [])],
            limit=limit,
            next_cursor=page.get("next_cursor"),
        )

    @app.get(
        "/v1/jobs/{job_id}",
        tags=["jobs"],
        dependencies=[Depends(authorize)],
        response_model=JobResponse,
    )
    def get_job(job_id: str) -> JobResponse:
        record = actual_manager.get_job_details(job_id)
        if not record:
            raise HTTPException(status_code=404, detail="job not found")
        return job_payload(record)

    @app.delete(
        "/v1/jobs/{job_id}",
        tags=["jobs"],
        dependencies=[Depends(authorize)],
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        response_model=None,
    )
    def delete_job(job_id: str) -> Any:
        try:
            actual_manager.delete_job(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="job not found") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/jobs/{job_id}/outputs",
        tags=["outputs"],
        dependencies=[Depends(authorize)],
        response_model=OutputListResponse,
    )
    def outputs(job_id: str) -> OutputListResponse:
        record = actual_manager.get_job_details(job_id)
        if not record:
            raise HTTPException(status_code=404, detail="job not found")
        try:
            files = actual_manager.output_files(job_id)
        except OutputExpiredError:
            raise HTTPException(status_code=410, detail="job outputs have expired") from None
        return OutputListResponse(
            job_id=job_id,
            files=files,
            archive_url=f"/v1/jobs/{job_id}/archive",
            manifest_url=f"/v1/jobs/{job_id}/manifest",
            expires_at=record.get("output_expires_at"),
        )

    @app.get(
        "/v1/jobs/{job_id}/manifest",
        tags=["outputs"],
        dependencies=[Depends(authorize)],
        response_model=ManifestResponse,
    )
    def manifest(job_id: str) -> ManifestResponse:
        try:
            return ManifestResponse(**actual_manager.manifest(job_id))
        except OutputExpiredError:
            raise HTTPException(status_code=410, detail="job outputs have expired") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="job not found") from None

    @app.get(
        "/v1/jobs/{job_id}/files/{relative_path:path}",
        tags=["outputs"],
        dependencies=[Depends(authorize)],
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Published output file",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    def output_file(job_id: str, relative_path: str) -> Any:
        record = actual_manager.get_job_details(job_id)
        if not record:
            raise HTTPException(status_code=404, detail="job not found")
        if record.get("state") not in {"succeeded", "failed", "interrupted"}:
            raise HTTPException(status_code=409, detail="job has not finished")
        try:
            lease = actual_manager.acquire_download_lease(
                job_id, relative_path, holder=f"file:{uuid.uuid4()}"
            )
        except OutputExpiredError:
            raise HTTPException(status_code=410, detail="job outputs have expired") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="output file not found") from None
        except PermissionError:
            raise HTTPException(status_code=400, detail="invalid output path") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            handle, size_bytes = actual_manager.open_output_file(job_id, relative_path)
        except FileNotFoundError:
            lease.release()
            raise HTTPException(status_code=404, detail="output file not found") from None
        except PermissionError:
            lease.release()
            raise HTTPException(status_code=400, detail="invalid output path") from None
        except ValueError as exc:
            lease.release()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        download_name = Path(relative_path).name
        media_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        chunks = file_chunks(handle, lease)
        return CloseableStreamingResponse(
            chunks,
            close=chunks.close,
            media_type=media_type,
            headers={
                "Content-Length": str(size_bytes),
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(download_name)
                ),
            },
        )

    @app.get(
        "/v1/jobs/{job_id}/archive",
        tags=["outputs"],
        dependencies=[Depends(authorize)],
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Streaming ZIP archive of published outputs",
                "content": {
                    "application/zip": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    def archive(job_id: str) -> Any:
        record = actual_manager.get_job_details(job_id)
        if not record:
            raise HTTPException(status_code=404, detail="job not found")
        if record.get("state") not in {"succeeded", "failed", "interrupted"}:
            raise HTTPException(status_code=409, detail="job has not finished")
        if record.get("output_deleted_at"):
            raise HTTPException(status_code=410, detail="job outputs have expired")
        lease = None
        try:
            lease = actual_manager.acquire_download_lease(
                job_id, "__archive__", holder=f"archive:{uuid.uuid4()}"
            )
            files = actual_manager.output_files(job_id)
            if hasattr(actual_manager, "resolve_output_root"):
                output_root = actual_manager.resolve_output_root(job_id)
            else:  # compatibility for adapters implementing the v1 manager surface
                output_root = Path(str(record["output_dir"]))
            preflight_archive(
                output_root,
                files,
                lease=lease,
                max_total_bytes=actual_config.max_output_bytes,
            )
            stream = iter_archive(
                output_root,
                files,
                lease=lease,
                lease_renew_seconds=getattr(
                    lease,
                    "renew_interval_seconds",
                    min(30.0, actual_config.download_lease_seconds / 2),
                ),
                max_total_bytes=actual_config.max_output_bytes,
            )
        except OutputExpiredError:
            if lease is not None:
                lease.release()
            raise HTTPException(status_code=410, detail="job outputs have expired") from None
        except FileNotFoundError:
            if lease is not None:
                lease.release()
            raise HTTPException(status_code=404, detail="job not found") from None
        except (ArchiveError, PermissionError, RuntimeError, ValueError) as exc:
            if lease is not None:
                lease.release()
            raise HTTPException(status_code=409, detail=str(exc)) from None

        return CloseableStreamingResponse(
            stream,
            close=stream.close,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=docling-{job_id}.zip"
                ),
                "Cache-Control": "no-store",
            },
        )

    @app.get(
        "/v1/system/storage",
        tags=["system"],
        dependencies=[Depends(authorize)],
        response_model=StorageResponse,
    )
    def storage() -> StorageResponse:
        return StorageResponse(**actual_manager.storage_status())

    @app.get(
        "/v1/system/config",
        tags=["system"],
        dependencies=[Depends(authorize)],
        response_model=SystemConfigResponse,
    )
    def get_system_config() -> SystemConfigResponse:
        if not hasattr(actual_manager, "runtime_config_snapshot"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="runtime config is currently unavailable",
            )
        return _system_config_response(actual_manager.runtime_config_snapshot())

    @app.patch(
        "/v1/system/config",
        tags=["system"],
        dependencies=[Depends(authorize)],
        response_model=SystemConfigResponse,
    )
    def patch_system_config(
        request: SystemConfigPatchRequest,
    ) -> SystemConfigResponse:
        if not hasattr(actual_manager, "update_runtime_config"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="runtime config is currently unavailable",
            )
        changes = dict(request.changes)
        try:
            return _system_config_response(
                actual_manager.update_runtime_config(
                    request.revision,
                    changes,
                )
            )
        except RuntimeConfigConflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="runtime configuration changed; refresh and retry",
            ) from None
        except ValueError as exc:
            # Backend range validation is deliberately authoritative, while
            # malformed values are still a client error on the HTTP boundary.
            raise HTTPException(status_code=422, detail=str(exc)) from None

    def validate_event_types(event_types: list[str]) -> None:
        allowed = {
            "docling.job.succeeded",
            "docling.job.failed",
            "docling.job.interrupted",
        }
        if not event_types or any(event not in allowed for event in event_types):
            raise HTTPException(status_code=422, detail="invalid webhook event type")

    @app.post(
        "/v1/webhooks/subscriptions",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        response_model=WebhookSubscriptionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_webhook_subscription(
        request: WebhookSubscriptionCreate,
    ) -> WebhookSubscriptionResponse:
        validate_event_types(request.event_types)
        callback_url = str(request.callback_url)
        try:
            actual_manager.validate_webhook_url(callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        record = actual_manager.store.create_webhook_subscription(
            callback_url=callback_url,
            event_types=request.event_types,
            filters=request.filters,
            secret=request.secret,
            headers=request.headers,
            name=request.name,
            enabled=request.enabled,
        )
        if record.get("error") == "subscription_limit":
            raise HTTPException(status_code=429, detail="webhook subscription limit reached")
        return subscription_payload(record)

    @app.get(
        "/v1/webhooks/subscriptions",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        response_model=WebhookSubscriptionListResponse,
    )
    def list_webhook_subscriptions() -> WebhookSubscriptionListResponse:
        page = actual_manager.store.list_webhook_subscriptions(include_disabled=True)
        return WebhookSubscriptionListResponse(
            items=[subscription_payload(item) for item in page.get("items", [])],
            count=int(page.get("count", 0)),
        )

    @app.get(
        "/v1/webhooks/subscriptions/{subscription_id}",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        response_model=WebhookSubscriptionResponse,
    )
    def get_webhook_subscription(
        subscription_id: int,
    ) -> WebhookSubscriptionResponse:
        record = actual_manager.store.get_webhook_subscription(subscription_id)
        if not record:
            raise HTTPException(status_code=404, detail="webhook subscription not found")
        return subscription_payload(record)

    @app.patch(
        "/v1/webhooks/subscriptions/{subscription_id}",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        response_model=WebhookSubscriptionResponse,
    )
    def update_webhook_subscription(
        subscription_id: int,
        request: WebhookSubscriptionUpdate,
    ) -> WebhookSubscriptionResponse:
        values = request.model_dump(exclude_unset=True)
        if "event_types" in values:
            validate_event_types(values["event_types"])
        if values.get("callback_url") is not None:
            values["callback_url"] = str(values["callback_url"])
            try:
                actual_manager.validate_webhook_url(values["callback_url"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
        record = actual_manager.store.update_webhook_subscription(
            subscription_id, **values
        )
        if record.get("error") or not record:
            raise HTTPException(status_code=404, detail="webhook subscription not found")
        return subscription_payload(record)

    @app.delete(
        "/v1/webhooks/subscriptions/{subscription_id}",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        response_model=None,
    )
    def delete_webhook_subscription(subscription_id: int) -> Any:
        result = actual_manager.store.delete_webhook_subscription(subscription_id)
        if result.get("error"):
            raise HTTPException(status_code=404, detail="webhook subscription not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/webhooks/deliveries",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        response_model=WebhookDeliveryListResponse,
    )
    def list_webhook_deliveries(
        subscription_id: int | None = Query(default=None),
        delivery_status: str | None = Query(default=None, alias="status"),
        job_id: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> WebhookDeliveryListResponse:
        page = actual_manager.store.list_webhook_deliveries(
            subscription_id=subscription_id,
            status=delivery_status,
            job_id=job_id,
            cursor=cursor,
            limit=limit,
        )
        if page.get("error"):
            raise HTTPException(status_code=400, detail=str(page["error"]))
        return WebhookDeliveryListResponse(
            items=[WebhookDeliveryResponse(**item) for item in page.get("items", [])],
            count=int(page.get("count", 0)),
            next_cursor=page.get("next_cursor"),
        )

    @app.post(
        "/v1/webhooks/deliveries/{delivery_id}/retry",
        tags=["webhooks"],
        dependencies=[Depends(authorize)],
        response_model=WebhookRetryResponse,
    )
    def retry_webhook_delivery(delivery_id: int) -> WebhookRetryResponse:
        existing = actual_manager.store.get_webhook_delivery(delivery_id)
        if not existing:
            raise HTTPException(status_code=404, detail="webhook delivery not found")
        if existing.get("status") != "failed":
            raise HTTPException(status_code=409, detail="only failed deliveries can retry")
        record = actual_manager.store.retry_webhook_delivery(
            delivery_id, error="manual retry", retry_after_seconds=1
        )
        if record.get("error"):
            raise HTTPException(status_code=409, detail=str(record["error"]))
        return WebhookRetryResponse(delivery=WebhookDeliveryResponse(**record))

    @app.webhooks.post("docling-job-event", tags=["webhooks"])
    def outgoing_job_event(body: JobCloudEvent) -> None:
        """CloudEvents 1.0 payload delivered to registered callback URLs."""
        return None

    generated_openapi = app.openapi

    def documented_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = generated_openapi()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ProblemDetails"] = ProblemDetails.model_json_schema()
        schema["paths"]["/v1/jobs"]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]["schema"] = {
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "client_reference": {
                    "type": ["string", "null"],
                    "maxLength": 200,
                },
            },
        }
        webhook_content = schema["webhooks"]["docling-job-event"]["post"][
            "requestBody"
        ]["content"]
        event_schema = webhook_content.pop("application/json")
        webhook_content["application/cloudevents+json"] = event_schema
        app.openapi_schema = schema
        return schema

    app.openapi = documented_openapi

    return app


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("uvicorn is required; install docling-service[api]") from exc
    host = os.getenv("DOCLING_API_HOST", "127.0.0.1")
    port = int(os.getenv("DOCLING_API_PORT", "8000"))
    config = ReleaseConfig.from_env()
    config.ensure_directories()
    # The formal executable owns one API app/process, so it can safely align
    # Starlette's process-wide spool target with the managed temp lifecycle.
    os.environ["TMPDIR"] = str(config.temp_root)
    tempfile.tempdir = str(config.temp_root)
    uvicorn.run(create_app(config=config), host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
