"""Versioned HTTP API for the formal docling-service releases."""

from __future__ import annotations

import hmac
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .release import (
    RELEASE_VERSION,
    JobManager,
    ReleaseConfig,
    probe_backend,
    probe_formula_service,
)


def create_app(config: ReleaseConfig | None = None, manager: JobManager | None = None) -> Any:
    try:
        from fastapi import Depends, FastAPI, File, Header, HTTPException, status
        from fastapi.responses import FileResponse
    except ImportError as exc:  # pragma: no cover - exercised by packaging smoke tests
        raise RuntimeError(
            "HTTP dependencies are missing; install docling-service[api]"
        ) from exc

    actual_config = config or ReleaseConfig.from_env()
    actual_manager = manager or JobManager(actual_config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        actual_manager.shutdown()

    app = FastAPI(
        title="Local AI Lab Docling Service",
        version=RELEASE_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    def authorize(authorization: str | None = Header(default=None)) -> None:
        token = actual_config.api_token
        if token is None:
            return
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.get("/healthz", tags=["system"])
    def healthz() -> Any:
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
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)
        return payload

    @app.get("/v1/capabilities", tags=["system"], dependencies=[Depends(authorize)])
    def capabilities() -> dict[str, Any]:
        return actual_config.public_capabilities()

    @app.post(
        "/v1/jobs",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["jobs"],
        dependencies=[Depends(authorize)],
    )
    async def create_job(file: Any = File(...)) -> dict[str, Any]:
        original_name = Path(file.filename or "document.pdf").name
        if not original_name.casefold().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="only PDF uploads are supported")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="docling-upload-", suffix=".pdf", dir=actual_config.input_root
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
            record = actual_manager.create_job(temporary, original_name)
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()
        return {
            "job_id": record.job_id,
            "state": record.state,
            "status_url": f"/v1/jobs/{record.job_id}",
            "outputs_url": f"/v1/jobs/{record.job_id}/outputs",
        }

    @app.get("/v1/jobs/{job_id}", tags=["jobs"], dependencies=[Depends(authorize)])
    def get_job(job_id: str) -> dict[str, Any]:
        record = actual_manager.get_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "job_id": record.job_id,
            "state": record.state,
            "original_name": record.original_name,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "exit_code": record.exit_code,
            "error": record.error,
            "outputs_url": f"/v1/jobs/{record.job_id}/outputs",
        }

    @app.get(
        "/v1/jobs/{job_id}/outputs",
        tags=["outputs"],
        dependencies=[Depends(authorize)],
    )
    def outputs(job_id: str) -> dict[str, Any]:
        if actual_manager.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job_id": job_id, "files": actual_manager.output_files(job_id)}

    @app.get(
        "/v1/jobs/{job_id}/files/{relative_path:path}",
        tags=["outputs"],
        dependencies=[Depends(authorize)],
    )
    def output_file(job_id: str, relative_path: str) -> Any:
        try:
            path = actual_manager.resolve_output_file(job_id, relative_path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="output file not found") from None
        except PermissionError:
            raise HTTPException(status_code=400, detail="invalid output path") from None
        return FileResponse(path, filename=path.name)

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
