#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
ROTATE_CHUNK_SIZE = 65536
FORCE_KILL_GRACE_SECONDS = 3.0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("must be greater than 0")
    return parsed


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return _positive_int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer: {raw}") from exc


def _rotate_log_file(path: Path, backup_count: int) -> None:
    base_path = str(path)
    oldest = Path(f"{base_path}.{backup_count}")
    oldest.unlink(missing_ok=True)

    for index in range(backup_count - 1, 0, -1):
        source = Path(f"{base_path}.{index}")
        destination = Path(f"{base_path}.{index + 1}")
        if source.exists():
            source.replace(destination)
    destination = Path(f"{base_path}.1")
    if path.exists():
        path.replace(destination)


def _rotate_bounded_existing_log(path: Path, max_bytes: int, backup_count: int) -> None:
    """Keep only a bounded tail when adopting a legacy unbounded log."""

    size = path.stat().st_size
    if size < max_bytes:
        return
    with path.open("rb") as source:
        source.seek(max(0, size - max_bytes))
        tail = source.read(max_bytes)
    with path.open("wb") as destination:
        destination.write(tail)
    _rotate_log_file(path, backup_count)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a command with log rotation.")
    parser.add_argument(
        "--log-path",
        required=True,
        help="Target log path for combined stdout/stderr output.",
    )
    parser.add_argument(
        "--max-bytes",
        type=_positive_int,
        default=None,
        help="Maximum size in bytes for a single log file.",
    )
    parser.add_argument(
        "--backup-count",
        type=_positive_int,
        default=None,
        help="Number of rotated backups to keep.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to execute; use -- before command.",
    )
    args = parser.parse_args()
    if not args.command:
        parser.error("Missing command. Use -- <command>.")
    return args


def _main() -> int:
    args = _parse_args()

    max_bytes = args.max_bytes or _env_int(
        "DOCLING_MACOS_LOG_MAX_BYTES",
        DEFAULT_MAX_BYTES,
    )
    backup_count = args.backup_count or _env_int(
        "DOCLING_MACOS_LOG_BACKUP_COUNT",
        DEFAULT_BACKUP_COUNT,
    )

    if backup_count <= 0:
        raise RuntimeError("DOCLING_MACOS_LOG_BACKUP_COUNT must be positive")

    log_path = Path(args.log_path).expanduser()
    if args.command[0] == "--":
        command = args.command[1:]
    else:
        command = args.command

    if not command:
        raise RuntimeError("Command list is empty.")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size >= max_bytes:
        _rotate_bounded_existing_log(log_path, max_bytes, backup_count)

    handle = log_path.open("ab")
    log_size = log_path.stat().st_size
    process = None
    return_code = 0
    termination_signal: int | None = None
    force_kill_started = threading.Event()

    def write_to_log(data: bytes) -> None:
        nonlocal log_size, handle

        offset = 0
        remaining = len(data)
        while remaining:
            if max_bytes <= 0:
                handle.write(data[offset:])
                handle.flush()
                log_size += remaining
                return

            if log_size >= max_bytes:
                handle.close()
                _rotate_log_file(log_path, backup_count)
                handle = log_path.open("ab")
                log_size = 0

            chunk_size = min(max_bytes - log_size, remaining)
            if chunk_size <= 0:
                handle.close()
                _rotate_log_file(log_path, backup_count)
                handle = log_path.open("ab")
                log_size = 0
                continue
            chunk = data[offset : offset + chunk_size]
            handle.write(chunk)
            handle.flush()
            log_size += len(chunk)
            offset += chunk_size
            remaining -= chunk_size

    def request_shutdown(signal_number: int) -> None:
        nonlocal termination_signal
        termination_signal = signal_number
        if process is None:
            return
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            return
        except OSError:
            return

        if force_kill_started.is_set():
            return
        force_kill_started.set()

        def force_kill_after_grace() -> None:
            time.sleep(FORCE_KILL_GRACE_SECONDS)
            if process is None or process.poll() is not None:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                return

        threading.Thread(
            target=force_kill_after_grace,
            daemon=True,
            name="docling-log-wrapper-kill",
        ).start()

    signal.signal(signal.SIGTERM, lambda _signum, _frame: request_shutdown(signal.SIGTERM))
    signal.signal(signal.SIGINT, lambda _signum, _frame: request_shutdown(signal.SIGINT))

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=True,
    )

    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(ROTATE_CHUNK_SIZE)
            if not chunk:
                break
            write_to_log(chunk)
        return_code = process.wait()
    finally:
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if process is not None:
            if process.stderr is not None:
                process.stderr.close()
            if process.stdout is not None:
                process.stdout.close()
        handle.close()

    if termination_signal is not None:
        return 128 + termination_signal
    return return_code


if __name__ == "__main__":
    sys.exit(_main())
