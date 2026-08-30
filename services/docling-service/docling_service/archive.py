"""Streaming archive helpers for exporting job outputs as ZIP."""

from __future__ import annotations

import hashlib
import io
import json
import time
import queue
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .lifecycle import open_relative_file


class ArchiveError(RuntimeError):
    """Raised when a job output cannot be safely streamed into an archive."""


class ArchiveChangedError(ArchiveError):
    """Raised when a manifest entry no longer matches its on-disk file."""


_END = object()
_ZIP_EPOCH_DATETIME = (1980, 1, 1, 0, 0, 0)
_ZIP_REGULAR_FILE_MODE = 0o100644
_DEFAULT_MAX_ENTRIES = 1000
_DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def _call_if_exists(target: Any, method: str) -> None:
    callback = getattr(target, method, None)
    if callable(callback):
        callback()


@dataclass(frozen=True)
class _ManifestEntry:
    path: str
    size_bytes: int
    sha256: str
    media_type: str


def _build_zip_info(path: str, file_size: int | None = None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=path, date_time=_ZIP_EPOCH_DATETIME)
    info.create_system = 3
    info.external_attr = (_ZIP_REGULAR_FILE_MODE << 16)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.flag_bits |= 0x800
    if file_size is not None:
        info.file_size = file_size
    return info


def _normalize_posix_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ArchiveError("manifest path must be non-empty text")
    if "\\" in raw:
        raise ArchiveError(f"manifest path is not POSIX-safe: {raw!r}")
    if raw.startswith("/"):
        raise ArchiveError(f"manifest path cannot be absolute: {raw!r}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveError(f"manifest path is invalid: {raw!r}")
    return "/".join(parts)


def _coerce_manifest_entries(manifest: Sequence[Mapping[str, Any]]) -> list[_ManifestEntry]:
    if isinstance(manifest, (str, bytes, bytearray)) or not isinstance(manifest, Sequence):
        raise ArchiveError("manifest must be a sequence")
    normalized: dict[str, _ManifestEntry] = {}
    for raw_item in manifest:
        if not isinstance(raw_item, Mapping):
            raise ArchiveError("manifest entries must be mappings")
        try:
            path = _normalize_posix_path(raw_item["path"])  # type: ignore[arg-type]
            size = int(raw_item["size_bytes"])  # type: ignore[arg-type]
            sha = str(raw_item["sha256"])  # type: ignore[arg-type]
            media_type = str(raw_item["media_type"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError("invalid manifest entry") from exc
        if size < 0:
            raise ArchiveError(f"invalid size for {path!r}")
        if len(sha) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in sha):
            raise ArchiveError(f"invalid sha256 for {path!r}")
        if path in normalized:
            raise ArchiveError(f"duplicate manifest path: {path!r}")
        normalized[path] = _ManifestEntry(
            path=path,
            size_bytes=size,
            sha256=sha.lower(),
            media_type=media_type,
        )
    return [normalized[path] for path in sorted(normalized)]


def _resolve_and_validate_file(root: Path, entry: _ManifestEntry) -> Path:
    if root.absolute().is_symlink():
        raise ArchiveError("job root is a symlink")
    root = root.resolve()
    candidate = (root / entry.path).resolve()
    if not candidate.is_relative_to(root):
        raise ArchiveError(f"manifest path escapes root: {entry.path!r}")
    current = root
    for part in entry.path.split("/"):
        current = current / part
        if current.is_symlink():
            raise ArchiveError(f"manifest path is a symlink: {entry.path!r}")
    if not candidate.is_file():
        raise ArchiveError(f"output file missing or not regular file: {entry.path!r}")
    if current.is_symlink():
        raise ArchiveError(f"output path is a symlink: {entry.path!r}")
    return candidate


def _read_file_chunks(root: Path, entry: _ManifestEntry, chunk_size: int) -> "Iterator[bytes]":
    expected_size = entry.size_bytes
    expected_sha = entry.sha256
    digest = hashlib.sha256()
    total = 0
    try:
        handle = open_relative_file(root, entry.path)
    except (OSError, PermissionError) as exc:
        raise ArchiveError(f"cannot safely open output file: {entry.path!r}") from exc
    with handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > expected_size:
                raise ArchiveChangedError(f"size changed for {entry.path!r}")
            yield chunk
    if total != expected_size:
        raise ArchiveChangedError(f"size changed for {entry.path!r}")
    if digest.hexdigest() != expected_sha:
        raise ArchiveChangedError(f"sha256 changed for {entry.path!r}")


class _QueueSink(io.RawIOBase):
    """A write-only, non-seekable sink that streams bytes to a bounded queue."""

    def __init__(self, target: "queue.Queue[bytes | object]", cancelled: threading.Event) -> None:
        self._target = target
        self._cancelled = cancelled
        self._closed = False

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:  # type: ignore[override]
        if self._closed or self._cancelled.is_set():
            raise RuntimeError("archive stream cancelled")
        block = bytes(data)
        if not block:
            return 0
        while True:
            if self._cancelled.is_set():
                raise RuntimeError("archive stream cancelled")
            try:
                self._target.put(block, timeout=0.05)
                return len(block)
            except queue.Full:
                continue

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True


class _ArchiveIterator(Iterator[bytes]):
    def __init__(
        self,
        root: Path,
        manifest: Sequence[Mapping[str, Any]],
        *,
        include_source_pdf: bool = False,
        chunk_size: int = 262144,
        max_queue_size: int = 16,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
        lease: object | None = None,
        lease_renew_seconds: float = 0.0,
    ) -> None:
        self._root = root
        self._chunk_size = chunk_size
        self._max_queue_size = max_queue_size
        self._lease = lease
        self._lease_renew_seconds = lease_renew_seconds

        self._entries = _coerce_manifest_entries(manifest)
        if not include_source_pdf:
            self._entries = [entry for entry in self._entries if entry.path != "source.pdf"]

        if max_entries <= 0:
            raise ArchiveError("max_entries must be positive")
        if max_total_bytes <= 0:
            raise ArchiveError("max_total_bytes must be positive")
        if len(self._entries) > max_entries:
            raise ArchiveError(f"manifest entries exceed max_entries ({max_entries})")
        total_size = sum(entry.size_bytes for entry in self._entries)
        if total_size > max_total_bytes:
            raise ArchiveError(f"manifest total bytes exceed max_total_bytes ({max_total_bytes})")

        self._queue: "queue.Queue[bytes | object]" = queue.Queue(maxsize=self._max_queue_size)
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._thread_error: BaseException | None = None
        self._closed = False
        self._lease_released = False
        self._lease_lock = threading.Lock()
        self._last_renew = 0.0

        self._thread = threading.Thread(
            target=self._produce,
            daemon=True,
            name="docling-archive",
        )
        self._thread.start()

    def __iter__(self) -> "_ArchiveIterator":
        return self

    def __next__(self) -> bytes:
        while True:
            if self._closed:
                raise StopIteration
            try:
                chunk_or_end = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._done.is_set():
                    error = self._thread_error
                    if error is not None:
                        raise error
                    raise StopIteration
                continue
            if chunk_or_end is _END:
                self._closed = True
                if self._thread_error is not None:
                    raise self._thread_error
                raise StopIteration
            return chunk_or_end  # type: ignore[return-value]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancelled.set()
        _call_if_exists(self._lease, "cancel")
        self._release_lease()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _renew_lease(self) -> None:
        if self._lease is None:
            return
        if self._lease_renew_seconds <= 0:
            _call_if_exists(self._lease, "renew")
            return
        now = time.monotonic()
        elapsed = now - self._last_renew
        if elapsed < self._lease_renew_seconds and self._last_renew != 0.0:
            return
        self._last_renew = now
        _call_if_exists(self._lease, "renew")

    def _release_lease(self) -> None:
        with self._lease_lock:
            if self._lease_released:
                return
            self._lease_released = True
        _call_if_exists(self._lease, "release")

    def _produce(self) -> None:
        root = self._root
        if self._root.absolute().is_symlink() or not self._root.is_dir():
            self._thread_error = ArchiveError("job root is not a directory")
            self._done.set()
            self._try_push(_END)
            self._release_lease()
            return

        sink = _QueueSink(self._queue, self._cancelled)
        try:
            with zipfile.ZipFile(
                sink,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
            ) as archive:
                for entry in self._entries:
                    if self._cancelled.is_set():
                        return
                    _resolve_and_validate_file(root, entry)
                    info = _build_zip_info(entry.path)
                    with archive.open(info, "w") as zf:
                        for chunk in _read_file_chunks(root, entry, self._chunk_size):
                            if self._cancelled.is_set():
                                return
                            zf.write(chunk)
                            self._renew_lease()

                manifest_payload = {
                    "files": [
                        {
                            "path": entry.path,
                            "size_bytes": entry.size_bytes,
                            "sha256": entry.sha256,
                            "media_type": entry.media_type,
                        }
                        for entry in self._entries
                    ]
                }
                manifest_data = json.dumps(
                    manifest_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                manifest_info = _build_zip_info("manifest.json")
                archive.writestr(manifest_info, manifest_data)
                self._renew_lease()
        except ArchiveError as exc:
            self._thread_error = exc
        except Exception as exc:  # noqa: BLE001
            self._thread_error = ArchiveError(f"archive failed: {exc}")
        finally:
            self._done.set()
            self._try_push(_END)
            self._release_lease()

    def _try_push(self, item: object) -> None:
        while not self._cancelled.is_set():
            try:
                self._queue.put(item, timeout=0.05)
                return
            except queue.Full:
                continue


def iter_archive(
    job_root: Path,
    manifest: Sequence[Mapping[str, Any]],
    *,
    include_source_pdf: bool = False,
    chunk_size: int = 262144,
    max_queue_size: int = 16,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    lease: object | None = None,
    lease_renew_seconds: float = 0.0,
) -> Iterator[bytes]:
    """
    Return a bounded streaming iterator of ZIP bytes for selected job outputs.

    The ZIP includes all non-directory regular files listed by the manifest, plus a
    generated manifest.json. Items are sorted by relative POSIX path.
    """

    return _ArchiveIterator(
        Path(job_root),
        manifest,
        include_source_pdf=include_source_pdf,
        chunk_size=chunk_size,
        max_queue_size=max_queue_size,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
        lease=lease,
        lease_renew_seconds=lease_renew_seconds,
    )


def preflight_archive(
    job_root: Path,
    manifest: Sequence[Mapping[str, Any]],
    *,
    include_source_pdf: bool = False,
    chunk_size: int = 262144,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    lease: object | None = None,
) -> None:
    """Verify every entry before an HTTP response commits status 200."""

    entries = _coerce_manifest_entries(manifest)
    if not include_source_pdf:
        entries = [entry for entry in entries if entry.path != "source.pdf"]
    if max_entries <= 0 or len(entries) > max_entries:
        raise ArchiveError(f"manifest entries exceed max_entries ({max_entries})")
    if max_total_bytes <= 0 or sum(entry.size_bytes for entry in entries) > max_total_bytes:
        raise ArchiveError(f"manifest total bytes exceed max_total_bytes ({max_total_bytes})")
    root = Path(job_root)
    if root.absolute().is_symlink() or not root.is_dir():
        raise ArchiveError("job root is not a directory")
    for entry in entries:
        _resolve_and_validate_file(root, entry)
        for _chunk in _read_file_chunks(root, entry, chunk_size):
            _call_if_exists(lease, "renew")


__all__ = [
    "ArchiveError",
    "ArchiveChangedError",
    "iter_archive",
    "preflight_archive",
]
