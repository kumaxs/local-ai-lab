"""Versioned HTTP API for the formal docling-service releases."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    ReleaseConfig,
    probe_backend,
    probe_formula_service,
)


LOGGER = logging.getLogger(__name__)


class _RequestBodyTooLarge(Exception):
    pass


class _RequestBodyLimitMiddleware:
    """Bound multipart request bytes before Starlette spools an upload."""

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not (
            scope.get("method") == "POST" and scope.get("path") == "/v1/jobs"
        ):
            await self.app(scope, receive, send)
            return

        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == b"content-length":
                try:
                    if int(raw_value) > self.max_body_bytes:
                        await self._reject(scope, send)
                        return
                except ValueError:
                    pass

        received = 0

        async def bounded_receive() -> Any:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, send)

    @staticmethod
    async def _reject(scope: Any, send: Any) -> None:
        payload = json.dumps(
            {
                "type": "urn:docling:error:request_too_large",
                "title": "Request body is too large",
                "status": 413,
                "detail": "multipart request exceeds the configured upload limit",
                "instance": scope.get("path", "/v1/jobs"),
                "code": "request_too_large",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
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
        from fastapi.responses import JSONResponse, StreamingResponse
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError as exc:  # pragma: no cover - exercised by packaging smoke tests
        raise RuntimeError(
            "HTTP dependencies are missing; install docling-service[api]"
        ) from exc

    actual_config = config or ReleaseConfig.from_env()
    actual_manager = manager or JobManager(actual_config)

    @asynccontextmanager
    async def lifespan(_app: Any):
        yield
        actual_manager.shutdown()

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

    def job_payload(record: dict[str, Any]) -> JobResponse:
        job_id = str(record["job_id"])
        if record.get("output_deleted_at"):
            artifact_state = "deleted"
        elif record.get("state") in {"queued", "running"}:
            artifact_state = "pending"
        elif Path(str(record.get("output_dir", ""))).is_dir():
            artifact_state = "available"
        else:
            artifact_state = "expired"
        return JobResponse(
            **record,
            artifact_state=artifact_state,
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

    def file_chunks(path: Path, lease: Any):
        try:
            with path.open("rb") as handle:
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
        try:
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
            temporary.unlink(missing_ok=True)
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
        return OutputListResponse(
            job_id=job_id,
            files=actual_manager.output_files(job_id),
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
        try:
            lease = actual_manager.acquire_download_lease(
                job_id, relative_path, holder=f"file:{uuid.uuid4()}"
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="output file not found") from None
        except PermissionError:
            raise HTTPException(status_code=400, detail="invalid output path") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            path = actual_manager.resolve_output_file(job_id, relative_path)
        except FileNotFoundError:
            lease.release()
            raise HTTPException(status_code=404, detail="output file not found") from None
        except PermissionError:
            lease.release()
            raise HTTPException(status_code=400, detail="invalid output path") from None
        except ValueError as exc:
            lease.release()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return StreamingResponse(
            file_chunks(path, lease),
            media_type=media_type,
            headers={
                "Content-Length": str(path.stat().st_size),
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(path.name)
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
            preflight_archive(
                Path(str(record["output_dir"])),
                files,
                lease=lease,
                max_total_bytes=actual_config.max_output_bytes,
            )
            stream = iter_archive(
                Path(str(record["output_dir"])),
                files,
                lease=lease,
                lease_renew_seconds=min(
                    30.0, actual_config.download_lease_seconds / 2
                ),
                max_total_bytes=actual_config.max_output_bytes,
            )
        except FileNotFoundError:
            if lease is not None:
                lease.release()
            raise HTTPException(status_code=410, detail="job outputs have expired") from None
        except (ArchiveError, RuntimeError, ValueError) as exc:
            if lease is not None:
                lease.release()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return StreamingResponse(
            stream,
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
    uvicorn.run(create_app(), host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
