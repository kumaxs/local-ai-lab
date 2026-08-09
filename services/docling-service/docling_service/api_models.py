"""Typed HTTP contract models for the release API."""

from __future__ import annotations

from typing import Any, Literal

import json

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


JobState = Literal["queued", "running", "succeeded", "failed", "interrupted"]


def _checked_webhook_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > 16:
        raise ValueError("webhook headers are limited to 16 entries")
    reserved = {"host", "content-length", "transfer-encoding", "connection", "content-type"}
    for name, value in headers.items():
        lowered = name.casefold()
        if (
            not name
            or lowered in reserved
            or lowered.startswith("x-docling-")
            or "\r" in name
            or "\n" in name
            or "\r" in value
            or "\n" in value
            or len(name) > 128
            or len(value) > 4096
        ):
            raise ValueError(f"unsafe webhook header: {name!r}")
    if len(json.dumps(headers, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
        raise ValueError("webhook headers exceed 16 KiB")
    return headers


def _checked_webhook_filters(filters: dict[str, Any]) -> dict[str, Any]:
    if len(filters) > 16:
        raise ValueError("webhook filters are limited to 16 entries")

    def depth(value: Any, level: int = 0) -> int:
        if level > 4:
            return level
        if isinstance(value, dict):
            return max((depth(item, level + 1) for item in value.values()), default=level)
        if isinstance(value, list):
            return max((depth(item, level + 1) for item in value), default=level)
        return level

    if depth(filters) > 4:
        raise ValueError("webhook filters exceed nesting depth 4")
    if len(json.dumps(filters, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
        raise ValueError("webhook filters exceed 16 KiB")
    return filters


class ProblemDetails(BaseModel):
    """RFC 9457 problem details returned by every API error path."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str | None = None
    code: str | None = None


class JobLinks(BaseModel):
    status: str
    outputs: str
    manifest: str
    archive: str


class JobCreateResponse(BaseModel):
    job_id: str
    state: JobState
    status_url: str
    outputs_url: str
    manifest_url: str
    archive_url: str
    idempotent_replay: bool = False


class JobResponse(BaseModel):
    job_id: str
    state: JobState
    original_name: str
    client_reference: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    input_size_bytes: int = 0
    output_size_bytes: int = 0
    input_expires_at: str | None = None
    output_expires_at: str | None = None
    tombstone_expires_at: str | None = None
    input_deleted_at: str | None = None
    output_deleted_at: str | None = None
    deleted_at: str | None = None
    artifact_state: Literal["pending", "available", "expired", "deleted"]
    outputs_url: str
    links: JobLinks


class JobListResponse(BaseModel):
    items: list[JobResponse]
    limit: int
    next_cursor: str | None = None


class OutputFile(BaseModel):
    path: str
    size_bytes: int
    sha256: str
    media_type: str
    download_url: str
    expires_at: str | None = None


class OutputListResponse(BaseModel):
    job_id: str
    files: list[OutputFile]
    archive_url: str
    manifest_url: str
    expires_at: str | None = None


class ManifestResponse(BaseModel):
    job_id: str
    manifest_sha256: str | None = None
    files: list[OutputFile]
    expires_at: str | None = None


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    release_version: str
    profile: str
    accepted_input_formats: list[str]
    max_upload_bytes: int
    max_concurrent_jobs: int
    max_pending_jobs: int
    max_output_bytes: int


class HealthResponse(BaseModel):
    ok: bool
    service: str
    profile: str
    backend: dict[str, Any]
    formula: dict[str, Any]


class StorageUsage(BaseModel):
    pending_jobs: int
    input_bytes: int
    output_bytes: int
    reserved_output_bytes: int
    total_managed_bytes: int
    filesystem_free_bytes: int


class StorageLimits(BaseModel):
    max_pending_jobs: int
    max_data_bytes: int
    max_output_bytes: int
    min_free_bytes: int


class StorageResponse(BaseModel):
    usage: StorageUsage
    limits: StorageLimits
    cleanup_interval_seconds: int


class WebhookSubscriptionCreate(BaseModel):
    callback_url: HttpUrl
    event_types: list[str] = Field(
        default_factory=lambda: ["docling.job.succeeded", "docling.job.failed"],
        min_length=1,
        max_length=3,
    )
    filters: dict[str, Any] = Field(default_factory=dict)
    secret: str = Field(min_length=16, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    name: str | None = Field(default=None, max_length=200)
    enabled: bool = True

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _checked_webhook_headers(value)

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _checked_webhook_filters(value)


class WebhookSubscriptionUpdate(BaseModel):
    callback_url: HttpUrl | None = None
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=3)
    filters: dict[str, Any] | None = None
    secret: str | None = Field(default=None, min_length=16, max_length=4096)
    headers: dict[str, str] | None = None
    name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _checked_webhook_headers(value) if value is not None else None

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _checked_webhook_filters(value) if value is not None else None


class WebhookSubscriptionResponse(BaseModel):
    id: int
    callback_url: str
    event_types: list[str]
    filters: dict[str, Any]
    headers: dict[str, str]
    name: str | None = None
    enabled: bool
    secret_set: bool
    created_at: str
    updated_at: str


class WebhookSubscriptionListResponse(BaseModel):
    items: list[WebhookSubscriptionResponse]
    count: int


class WebhookDeliveryResponse(BaseModel):
    id: int
    subscription_id: int
    job_id: str | None = None
    event_type: str
    event_id: str
    status: Literal["pending", "in_progress", "retrying", "succeeded", "failed"]
    attempts: int
    max_attempts: int
    next_attempt_at: str
    created_at: str
    updated_at: str
    last_error: str | None = None
    last_status_code: int | None = None


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]
    count: int
    next_cursor: str | None = None


class WebhookRetryResponse(BaseModel):
    delivery: WebhookDeliveryResponse


class JobWebhookData(BaseModel):
    job_id: str
    state: JobState
    original_name: str
    client_reference: str | None = None
    status_url: str
    outputs_url: str
    manifest_url: str
    archive_url: str


class JobCloudEvent(BaseModel):
    specversion: Literal["1.0"] = "1.0"
    id: str
    source: str
    type: str
    time: str
    datacontenttype: Literal["application/json"] = Field(
        default="application/json",
        description=(
            "Media type of the CloudEvent data value; the HTTP envelope uses "
            "application/cloudevents+json."
        ),
    )
    subject: str
    data: JobWebhookData


__all__ = [name for name in globals() if not name.startswith("_")]
