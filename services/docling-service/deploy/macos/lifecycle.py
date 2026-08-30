#!/usr/bin/env python3
"""Small, fail-closed lifecycle supervisor for the macOS release.

The shell entry points intentionally contain no process-management protocol.
This module owns the lock, process records, launch handshake, supervision and
log rotation.  Only the standard library is used so the installed release
venv can run it before the service package is importable.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import secrets
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


METADATA_VERSION = 2
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 3
DEFAULT_GRACE_SECONDS = 3.0
DEFAULT_READY_SECONDS = 30.0
DEFAULT_CURL_CONNECT_SECONDS = 2
DEFAULT_CURL_MAX_SECONDS = 5
MAX_LOG_BYTES = 1 << 30
MAX_LOG_BACKUPS = 100
MAX_TIMEOUT_SECONDS = 3600
MAX_PORT = 65535
MAX_PID = (1 << 31) - 1
MAX_METADATA_BYTES = 1 << 20
MAX_COMPAT_RECORD_BYTES = 4096
PS_BIN = "/bin/ps"
LSOF_BIN = "/usr/sbin/lsof"
CURL_BIN = "/usr/bin/curl"


class LifecycleError(RuntimeError):
    pass


class BusyError(LifecycleError):
    pass


class IdentityUnknown(LifecycleError):
    pass


class IdentityMismatch(LifecycleError):
    pass


def _port(value: str) -> int:
    try:
        return _bounded_positive(value, MAX_PORT, "port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _bounded_positive(value: str, maximum: int, label: str) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return parsed


def _env_bounded(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return _bounded_positive(raw, maximum, name)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


def _env_positive(name: str, default: int) -> int:
    """Compatibility helper for bounded timeout/count environment values."""

    return _env_bounded(name, default, MAX_TIMEOUT_SECONDS)


def _log_size(value: str) -> int:
    try:
        return _bounded_positive(value, MAX_LOG_BYTES, "max-bytes")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _backup_count(value: str) -> int:
    try:
        return _bounded_positive(value, MAX_LOG_BACKUPS, "backup-count")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _proc_start_ticks(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = raw.rsplit(")", 1)[1].strip().split()
        return suffix[19]
    except (OSError, IndexError, ValueError):
        return None


class _DarwinBsdInfo(ctypes.Structure):
    # Matches Apple's public struct proc_bsdinfo (sys/proc_info.h).  In
    # particular, pbi_pgid comes after the credential and command fields; an
    # abbreviated struct would silently turn a birth timestamp into junk.
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("ttydev", ctypes.c_uint32),
        ("ttypgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_tvsec", ctypes.c_uint64),
        ("start_tvusec", ctypes.c_uint64),
    ]


def _darwin_birth(pid: int) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _DarwinBsdInfo()
        # PROC_PIDTBSDINFO is 3 in Apple's libproc.h.
        size = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size < ctypes.sizeof(info) or int(info.pid) != pid:
            return None
        if not (0 < int(info.start_tvsec) < 4_000_000_000):
            return None
        return f"{int(info.start_tvsec)}.{int(info.start_tvusec):06d}"
    except (AttributeError, OSError, ctypes.ArgumentError, ValueError):
        return None


def _ps(pid: int, field: str) -> str | None:
    try:
        result = subprocess.run(
            [PS_BIN, "-p", str(pid), "-o", f"{field}="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityUnknown(f"unable to inspect PID {pid}") from exc
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def process_identity(pid: int) -> dict[str, Any] | None:
    """Return identity, None for verified death, raise for unknown inspection."""

    if pid <= 1 or pid > MAX_PID:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise IdentityUnknown(f"permission denied inspecting PID {pid}") from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        raise IdentityUnknown(f"unable to inspect PID {pid}") from exc
    state = _ps(pid, "stat")
    if state is None or state.startswith("Z"):
        return None
    command = _ps(pid, "command")
    lstart = _ps(pid, "lstart")
    if not command or not lstart:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return None
        raise IdentityUnknown(f"incomplete identity for PID {pid}")
    try:
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise IdentityUnknown(f"unable to inspect process group for PID {pid}") from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        raise IdentityUnknown(f"unable to inspect process group for PID {pid}") from exc
    precise_birth = _darwin_birth(pid) or _proc_start_ticks(pid)
    birth = precise_birth or lstart
    return {
        "pid": pid,
        "birth": birth,
        "birth_precise": precise_birth is not None,
        "lstart": lstart,
        "pgid": int(pgid),
        "sid": int(sid),
        "command": command,
        "state": state,
    }


def _identity_matches(
    expected: dict[str, Any],
    actual: dict[str, Any] | None,
    *,
    token: str | None = None,
    require_precise: bool = False,
) -> bool:
    if actual is None:
        return False
    if expected.get("birth") != actual.get("birth"):
        return False
    if require_precise and not actual.get("birth_precise", False):
        return False
    if expected.get("pgid") != actual.get("pgid"):
        return False
    if expected.get("sid") != actual.get("sid"):
        return False
    # ``token`` is supplied explicitly for the lifecycle supervisor.  A
    # service command may be a shell wrapper that execs a different final
    # executable, so an implicit expected_command substring would reject a
    # healthy child despite its precise birth/PGID/SID identity.
    expected_token = token
    if expected_token and expected_token not in str(actual.get("command", "")):
        return False
    return True


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_regular_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    # O_NONBLOCK is inert for regular files and prevents a hostile FIFO from
    # hanging status/stop before the fstat regular-file check can run.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError(f"unable to read {label}: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LifecycleError(f"refusing non-private {label}: {path}")
        if info.st_size > maximum:
            raise LifecycleError(f"{label} exceeds {maximum} bytes: {path}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise LifecycleError(f"{label} exceeds {maximum} bytes: {path}")
        return payload
    except OSError as exc:
        raise LifecycleError(f"unable to read {label}: {path}") from exc
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = _read_regular_bytes(path, maximum=MAX_METADATA_BYTES, label="metadata")
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"invalid metadata: {path}") from exc
    if not isinstance(value, dict) or value.get("version") != METADATA_VERSION:
        raise LifecycleError(f"unsupported metadata: {path}")
    if not isinstance(value.get("service"), str) or not value["service"]:
        raise LifecycleError(f"invalid service metadata: {path}")
    if not isinstance(value.get("instance"), str) or not value["instance"]:
        raise LifecycleError(f"invalid instance metadata: {path}")
    if value.get("state") not in {"starting", "running", "stopping", "exited", "failed"}:
        raise LifecycleError(f"invalid metadata state: {path}")
    supervisor = value.get("supervisor")
    if (
        not isinstance(supervisor, dict)
        or not isinstance(supervisor.get("pid"), int)
        or not 1 < supervisor["pid"] <= MAX_PID
    ):
        raise LifecycleError(f"invalid supervisor metadata: {path}")
    service_sid = value.get("service_sid")
    if not isinstance(service_sid, int) or not 1 < service_sid <= MAX_PID:
        raise LifecycleError(f"invalid service session metadata: {path}")
    port = value.get("port")
    if not isinstance(port, int) or not 1 <= port <= MAX_PORT:
        raise LifecycleError(f"invalid service port metadata: {path}")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str):
        raise LifecycleError(f"invalid service endpoint metadata: {path}")
    try:
        _validate_loopback_endpoint(endpoint, port)
    except LifecycleError as exc:
        raise LifecycleError(f"invalid service endpoint metadata: {path}") from exc
    for role in ("guard",):
        item = value.get(role)
        if item is not None and (
            not isinstance(item, dict)
            or not isinstance(item.get("pid"), int)
            or not 1 < item["pid"] <= MAX_PID
        ):
            raise LifecycleError(f"invalid {role} metadata: {path}")
    child = value.get("child")
    if child is not None and (
        not isinstance(child, dict)
        or not isinstance(child.get("pid"), int)
        or not 1 < child["pid"] <= MAX_PID
    ):
        raise LifecycleError(f"invalid child metadata: {path}")
    return value


@dataclass(frozen=True)
class Service:
    name: str
    script: Path
    port: int
    endpoint: str
    log_path: Path


def _validate_loopback_endpoint(endpoint: str, expected_port: int | None = None) -> None:
    if not isinstance(endpoint, str) or not endpoint or endpoint.strip() != endpoint:
        raise LifecycleError("invalid service endpoint")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise LifecycleError("invalid service endpoint") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or port is None
        or not 1 <= port <= MAX_PORT
        or (expected_port is not None and port != expected_port)
    ):
        raise LifecycleError("service endpoint must be an HTTP loopback URL")


class LifecycleLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "LifecycleLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            self.fd = os.open(self.path, flags, 0o600)
            lock_info = os.fstat(self.fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise LifecycleError(f"refusing non-regular lifecycle lock: {self.path}")
            os.fchmod(self.fd, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BusyError("another lifecycle operation is in progress") from exc
            if exc.errno == errno.ELOOP:
                raise LifecycleError(f"refusing symlinked lifecycle lock: {self.path}") from exc
            raise LifecycleError(f"unable to acquire lifecycle lock: {self.path}") from exc
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def _pid_file(runtime: Path, service: str) -> Path:
    return runtime / "pids" / f"{service}.pid"


def _instance_file(runtime: Path, service: str) -> Path:
    return runtime / "pids" / f"{service}.instance"


def _meta_file(runtime: Path, service: str) -> Path:
    return runtime / "pids" / f"{service}.meta.json"


def _read_service_metadata(path: Path, service: str) -> dict[str, Any]:
    metadata = _read_json(path)
    if metadata.get("service") != service:
        raise LifecycleError(f"metadata service mismatch: expected {service}")
    return metadata


def _write_fd_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    for _attempt in range(8):
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        temporary = candidate
        try:
            try:
                data = value.encode("utf-8")
                _write_fd_all(fd, data)
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(candidate, path)
            temporary = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
            return
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise LifecycleError(f"unable to reserve temporary record for {path}")


def _write_compat_records(runtime: Path, service: str, supervisor_pid: int, instance: str) -> None:
    # The two text files remain for operators/scripts from 1.1.0; metadata is
    # the only authority and these are written only after its reservation.
    for path, value in (
        (_pid_file(runtime, service), f"{supervisor_pid}\n"),
        (_instance_file(runtime, service), f"{instance}\n"),
    ):
        _atomic_text(path, value)


def _remove_records(runtime: Path, service: str) -> None:
    paths = (_pid_file(runtime, service), _instance_file(runtime, service), _meta_file(runtime, service))
    for path in paths:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LifecycleError(f"unable to inspect lifecycle record: {path}") from exc
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise LifecycleError(f"refusing non-file lifecycle record: {path}")
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise LifecycleError(f"unable to remove lifecycle record: {path}") from exc


def _session_members(sid: int, *, exclude: Iterable[int] = ()) -> list[int]:
    try:
        result = subprocess.run(
            [PS_BIN, "-axo", "pid=,stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityUnknown("unable to inspect process session") from exc
    if result.returncode != 0:
        raise IdentityUnknown("unable to inspect process session")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise IdentityUnknown("process-session inspection returned no data")
    excluded = set(exclude)
    members: list[int] = []
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            raise IdentityUnknown("process-session inspection returned malformed data")
        try:
            pid = int(fields[0])
        except ValueError as exc:
            raise IdentityUnknown("process-session inspection returned malformed PID") from exc
        if pid <= 1:
            continue
        if pid > MAX_PID:
            raise IdentityUnknown("process-session inspection returned out-of-range PID")
        if pid in excluded or fields[1].startswith("Z"):
            continue
        try:
            member_sid = os.getsid(pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise IdentityUnknown("unable to inspect process session") from exc
        if member_sid != sid:
            continue
        members.append(pid)
    return sorted(set(members))


def _signal_session(sid: int, *, exclude: Iterable[int], signum: int) -> None:
    members = _session_members(sid, exclude=exclude)
    for pid in members:
        # Revalidate immediately before signalling to narrow the unavoidable
        # macOS PID-enumeration race.  A PID already recycled outside the
        # service SID is skipped rather than signalled.
        actual = process_identity(pid)
        if actual is None:
            continue
        if int(actual["sid"]) != sid:
            continue
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise IdentityUnknown(f"permission denied signalling PID {pid}") from exc


def _signal_exact(
    expected: dict[str, Any],
    signum: int,
    *,
    label: str,
    token: str | None = None,
    service_sid: int | None = None,
    outside_service_sid: bool = False,
) -> bool:
    """Revalidate a recorded role immediately before a direct signal."""

    pid = int(expected["pid"])
    actual = process_identity(pid)
    if actual is None:
        return False
    if not _identity_matches(expected, actual, token=token, require_precise=True):
        raise IdentityMismatch(f"{label} identity changed before signal")
    if service_sid is not None:
        same_sid = int(actual["sid"]) == service_sid
        if same_sid == outside_service_sid:
            raise IdentityMismatch(f"{label} session changed before signal")
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise IdentityUnknown(f"permission denied signalling {label}") from exc
    return True


def _terminate_session(
    sid: int,
    *,
    exclude: Iterable[int] = (),
    grace: float = DEFAULT_GRACE_SECONDS,
) -> None:
    """Terminate every process in *sid*, preserving explicitly excluded PIDs."""

    excluded = set(exclude)
    _signal_session(sid, exclude=excluded, signum=signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _session_members(sid, exclude=excluded):
            return
        time.sleep(0.05)
    _signal_session(sid, exclude=excluded, signum=signal.SIGKILL)


def _wait_exit_code(status: int) -> int:
    try:
        return os.waitstatus_to_exitcode(status)
    except (AttributeError, ValueError) as exc:
        raise LifecycleError("invalid child wait status") from exc


def _shell_exit_code(code: int | None) -> int:
    if code is None:
        return 1
    return 128 + (-code) if code < 0 else code


def _reap_bounded(pid: int, timeout: float = DEFAULT_READY_SECONDS) -> int | None:
    """Reap a direct child and return its normalized exit code."""

    if pid <= 1:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        except InterruptedError:
            continue
        if waited_pid == pid:
            return _wait_exit_code(status)
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return None
    try:
        waited_pid, status = os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        return None
    return _wait_exit_code(status) if waited_pid == pid else None


def _terminate_process_bounded(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    """Terminate a supervisor and escalate to KILL without an unbounded wait."""

    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _service_identity(metadata: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    """Validate the recorded supervisor/session and, when live, the child."""

    supervisor = metadata.get("supervisor")
    child = metadata.get("child")
    sid = metadata.get("service_sid")
    if not isinstance(supervisor, dict) or not isinstance(child, dict) or not isinstance(sid, int) or sid <= 1:
        raise LifecycleError("incomplete service identity metadata")
    supervisor_actual = process_identity(int(supervisor["pid"]))
    if supervisor_actual is None or not _identity_matches(
        supervisor, supervisor_actual, token="lifecycle.py", require_precise=True
    ):
        raise IdentityMismatch("service supervisor identity changed")
    if int(supervisor_actual["sid"]) != sid:
        raise IdentityMismatch("service supervisor session changed")
    child_actual = process_identity(int(child["pid"]))
    if child_actual is not None:
        if not _identity_matches(child, child_actual, require_precise=True):
            raise IdentityMismatch("service child identity changed")
        if int(child_actual["sid"]) != sid:
            raise IdentityMismatch("service child session changed")
    return sid, supervisor, child_actual


def _cleanup_after_guard_exit(metadata: dict[str, Any], *, reap_child: bool = True) -> None:
    """Clean the service SID when the independent sentinel dies.

    The supervisor is the direct parent of the service child, so it can reap
    that child after the session has been terminated.  Callers without that
    parent relationship may pass ``reap_child=False``; they still refuse to
    guess at a recycled PID/SID and retain records when a live child cannot be
    proven to belong to this launch.
    """

    child = metadata.get("child") or {}
    if not isinstance(child, dict) or int(child.get("pid", 0)) <= 1:
        return
    sid, supervisor, child_actual = _service_identity(metadata)
    supervisor_pid = int(supervisor.get("pid", 0))
    _terminate_session(sid, exclude={supervisor_pid}, grace=0.5)
    if reap_child and supervisor_pid == os.getpid():
        child_pid = int(child["pid"])
        try:
            os.waitpid(child_pid, 0)
        except ChildProcessError:
            pass


def _assert_regular_log(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LifecycleError(f"unable to inspect log path: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LifecycleError(f"refusing non-regular log path: {path}")


def _open_log_fd(path: Path, flags: int) -> int:
    _assert_regular_log(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LifecycleError(f"refusing symlinked log path: {path}") from exc
        raise LifecycleError(f"unable to open log path: {path}") from exc
    try:
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LifecycleError(f"refusing non-regular log path: {path}")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rotate(path: Path, backups: int) -> None:
    for candidate in [path, *[Path(f"{path}.{index}") for index in range(1, backups + 1)]]:
        _assert_regular_log(candidate)
    Path(f"{path}.{backups}").unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = Path(f"{path}.{index}")
        if source.exists():
            source.replace(Path(f"{path}.{index + 1}"))
    if path.exists():
        path.replace(Path(f"{path}.1"))


class LogWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = _env_bounded("DOCLING_MACOS_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES, MAX_LOG_BYTES)
        self.backups = _env_bounded("DOCLING_MACOS_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUPS, MAX_LOG_BACKUPS)
        _assert_regular_log(self.path)
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= self.max_bytes:
            fd = _open_log_fd(self.path, os.O_RDONLY)
            try:
                source_size = os.fstat(fd).st_size
                os.lseek(fd, max(0, source_size - self.max_bytes), os.SEEK_SET)
                tail = os.read(fd, min(self.max_bytes, source_size))
            finally:
                os.close(fd)
            fd = _open_log_fd(self.path, os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(fd, tail)
                os.fsync(fd)
            finally:
                os.close(fd)
            _rotate(self.path, self.backups)
        fd = _open_log_fd(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        self.size = os.fstat(fd).st_size
        self.handle = os.fdopen(fd, "ab", buffering=0)

    def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            if self.size >= self.max_bytes:
                self.handle.close()
                _rotate(self.path, self.backups)
                fd = _open_log_fd(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
                self.handle = os.fdopen(fd, "ab", buffering=0)
                self.size = 0
            count = min(self.max_bytes - self.size, len(data) - offset)
            self.handle.write(data[offset : offset + count])
            self.handle.flush()
            self.size += count
            offset += count

    def close(self) -> None:
        self.handle.close()


def _health(endpoint: str, timeout: int, *, curl: str = CURL_BIN) -> bool:
    try:
        _validate_loopback_endpoint(endpoint)
    except LifecycleError:
        return False
    try:
        result = subprocess.run(
            [curl, "--connect-timeout", str(DEFAULT_CURL_CONNECT_SECONDS), "--max-time", str(DEFAULT_CURL_MAX_SECONDS), "-fsS", endpoint],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _health_owned(runtime: Path, service: Service) -> bool:
    metadata = _read_service_metadata(_meta_file(runtime, service.name), service.name)
    port = metadata.get("port")
    endpoint = metadata.get("endpoint")
    if not isinstance(port, int) or not isinstance(endpoint, str) or port != service.port or endpoint != service.endpoint:
        return False
    try:
        _validate_loopback_endpoint(endpoint, port)
    except LifecycleError:
        return False
    if not _health(endpoint, 1):
        return False
    supervisor = metadata.get("supervisor")
    if not isinstance(supervisor, dict):
        return False
    supervisor_actual = process_identity(int(supervisor["pid"]))
    if (
        supervisor_actual is None
        or not _identity_matches(supervisor, supervisor_actual, token="lifecycle.py", require_precise=True)
        or int(supervisor_actual["sid"]) != int(metadata["service_sid"])
    ):
        return False
    guard = metadata.get("guard")
    if not isinstance(guard, dict):
        return False
    guard_actual = process_identity(int(guard["pid"]))
    if (
        guard_actual is None
        or not _identity_matches(guard, guard_actual, require_precise=True)
        or int(guard_actual["sid"]) == int(metadata["service_sid"])
    ):
        return False
    child = metadata.get("child")
    if not isinstance(child, dict):
        return False
    child_actual = process_identity(int(child["pid"]))
    if (
        child_actual is None
        or not _identity_matches(child, child_actual, require_precise=True)
        or int(child_actual["sid"]) != int(metadata["service_sid"])
    ):
        return False
    listeners = _listener_pids(service.port)
    if not listeners:
        return False
    for pid in listeners:
        actual = process_identity(pid)
        if actual is None or int(actual["sid"]) != int(metadata["service_sid"]):
            return False
    return True


def _listener_pids(port: int) -> list[int]:
    if not 1 <= int(port) <= MAX_PORT:
        raise LifecycleError("invalid service port")
    try:
        result = subprocess.run(
            [LSOF_BIN, "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("cannot inspect listeners") from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode == 1 and not result.stderr.strip() and not lines:
        return []
    if result.returncode != 0:
        raise LifecycleError("cannot inspect listeners")
    if not lines:
        raise LifecycleError("listener inspection returned no data")
    pids: list[int] = []
    for line in lines:
        if not line.isdigit() or not 1 < int(line) <= MAX_PID:
            raise LifecycleError("listener inspection returned malformed PID")
        pids.append(int(line))
    return sorted(set(pids))


def _default_service_endpoint(service: str) -> tuple[int, str]:
    if service == "backend":
        port = _env_bounded("DOCLING_BACKEND_PORT", 5001, MAX_PORT)
        return port, f"http://127.0.0.1:{port}/version"
    port = _env_bounded("DOCLING_API_PORT", 8000, MAX_PORT)
    return port, f"http://127.0.0.1:{port}/healthz"


def _base_metadata(
    service: str,
    instance: str,
    supervisor: dict[str, Any],
    port: int,
    endpoint: str,
) -> dict[str, Any]:
    _validate_loopback_endpoint(endpoint, port)
    return {
        "version": METADATA_VERSION,
        "service": service,
        "instance": instance,
        "port": port,
        "endpoint": endpoint,
        "state": "starting",
        "seq": 1,
        "updated_at": time.time_ns(),
        "supervisor": supervisor,
        "guard": None,
        "child": None,
        "exit": None,
    }


def _update_metadata(path: Path, metadata: dict[str, Any], state: str | None = None, **extra: Any) -> None:
    next_value = dict(metadata)
    next_value.update(extra)
    if state is not None:
        next_value["state"] = state
    next_value["seq"] = int(next_value.get("seq", 0)) + 1
    next_value["updated_at"] = time.time_ns()
    _atomic_json(path, next_value)
    metadata.clear()
    metadata.update(next_value)


def _readline(fd: int, timeout: float) -> bytes:
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        raise LifecycleError("lifecycle handshake timed out")
    data = os.read(fd, 65536)
    if not data:
        raise LifecycleError("lifecycle handshake closed")
    return data.splitlines()[0]


def _read_byte(fd: int, timeout: float) -> bytes:
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        raise LifecycleError("lifecycle handshake timed out")
    return os.read(fd, 1)


def _test_gate(runtime: Path, service: str, stage: str) -> None:
    """Pause only when an explicit lifecycle test asks for a crash window."""

    if os.environ.get("DOCLING_LIFECYCLE_TEST_GATE") != stage:
        return
    marker = runtime / "pids" / f".{service}.{stage}.gate"
    marker.write_text(str(os.getpid()), encoding="utf-8")
    try:
        deadline = time.monotonic() + _env_positive("DOCLING_LIFECYCLE_TEST_GATE_TIMEOUT", 30)
        while marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        marker.unlink(missing_ok=True)


def _guard(
    service_sid: int,
    supervisor_expected: dict[str, Any],
    supervisor_pid: int,
    child_pid: int,
    control_fd: int,
    ready_fd: int,
    output_r: int,
    log_path: Path,
) -> int:
    """Independent sentinel that owns no service children.

    The supervisor owns and reaps the service child.  This process gets its
    own session so it survives a supervisor SIGKILL; its only authority is the
    validated service SID supplied by the supervisor.  EOF on the control pipe
    therefore means the supervisor died and the service SID must be cleaned.
    """

    writer: LogWriter | None = None
    control = None
    try:
        os.setsid()

        def supervisor_exclude() -> set[int]:
            try:
                supervisor_actual = process_identity(supervisor_pid)
            except IdentityUnknown:
                # The service SID is the cleanup authority.  An uninspectable
                # or recycled supervisor PID must never suppress cleanup of
                # the remaining session; only the exact original process is
                # preserved so it can commit terminal metadata.
                return set()
            if supervisor_actual is None:
                return set()
            if not _identity_matches(
                supervisor_expected,
                supervisor_actual,
                token="lifecycle.py",
                require_precise=True,
            ) or int(supervisor_actual["sid"]) != service_sid:
                return set()
            return {supervisor_pid}

        def terminate_service(grace: float = DEFAULT_GRACE_SECONDS) -> None:
            _terminate_session(service_sid, exclude=supervisor_exclude(), grace=grace)

        guard_pid = os.getpid()
        guard_identity = process_identity(guard_pid)
        if guard_identity is None:
            raise LifecycleError("guard identity unavailable")
        child_expected: dict[str, Any] | None = None
        child_deadline = time.monotonic() + DEFAULT_READY_SECONDS
        while time.monotonic() < child_deadline:
            candidate = process_identity(child_pid)
            if candidate is not None and int(candidate["sid"]) == service_sid and int(candidate["pgid"]) == child_pid:
                child_expected = candidate
                break
            time.sleep(0.01)
        if child_expected is None:
            raise IdentityMismatch("service child identity unavailable")
        os.write(ready_fd, (json.dumps({"guard": guard_identity, "sid": os.getsid(guard_pid)}) + "\n").encode())
        os.close(ready_fd)
        control = os.fdopen(control_fd, "rb", buffering=0)
        token = _read_byte(control_fd, DEFAULT_READY_SECONDS)
        if token != b"A":
            child_actual = process_identity(child_pid)
            if child_actual is not None and not _identity_matches(child_expected, child_actual, require_precise=True):
                raise IdentityMismatch("service child identity changed before ACK")
            terminate_service(grace=0.5)
            return 75

        writer = LogWriter(log_path)
        stop_requested = False

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGHUP, request_stop)
        os.set_blocking(output_r, False)
        while True:
            if stop_requested:
                child_actual = process_identity(child_pid)
                if child_actual is not None and not _identity_matches(child_expected, child_actual, require_precise=True):
                    raise IdentityMismatch("service child identity changed before stop")
                terminate_service()
                break
            watched = [control_fd]
            if output_r >= 0:
                watched.append(output_r)
            ready, _, _ = select.select(watched, [], [], 0.1)
            if control_fd in ready:
                token = control.read(1)
                # Both an explicit stop byte and supervisor EOF are terminal.
                if token != b"A":
                    child_actual = process_identity(child_pid)
                    if child_actual is not None and not _identity_matches(child_expected, child_actual, require_precise=True):
                        raise IdentityMismatch("service child identity changed before cleanup")
                    stop_requested = True
                else:
                    stop_requested = True
            if output_r >= 0 and output_r in ready:
                chunk = os.read(output_r, 65536)
                if chunk:
                    writer.write(chunk)
                else:
                    os.close(output_r)
                    output_r = -1
        return 0
    except BaseException:
        try:
            terminate_service(grace=0.5)
        except Exception:
            pass
        return 1
    finally:
        if writer is not None:
            writer.close()
        if control is not None:
            control.close()
        if output_r >= 0:
            try:
                os.close(output_r)
            except OSError:
                pass


def _supervise(args: argparse.Namespace) -> int:
    runtime = Path(args.runtime_dir).resolve()
    pids = runtime / "pids"
    pids.mkdir(parents=True, exist_ok=True)
    metadata_path = _meta_file(runtime, args.service)
    instance = args.instance
    # The release launcher already starts us in a fresh session.  The guarded
    # direct entry point used by tests (and older operators) may not, so make
    # the supervisor the service-session owner whenever possible.
    try:
        if os.getsid(os.getpid()) != os.getpid():
            os.setsid()
    except OSError as exc:
        raise LifecycleError("unable to establish service session") from exc
    supervisor = process_identity(os.getpid())
    if supervisor is None:
        raise LifecycleError("supervisor identity unavailable")
    command = json.loads(args.command_json)
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise LifecycleError("invalid service command")
    service_sid = int(supervisor["sid"])
    default_port, default_endpoint = _default_service_endpoint(args.service)
    port = int(getattr(args, "port", 0) or default_port)
    endpoint = str(getattr(args, "endpoint", "") or default_endpoint)
    _validate_loopback_endpoint(endpoint, port)
    metadata = _base_metadata(args.service, instance, supervisor, port, endpoint)
    metadata["service_sid"] = service_sid
    _atomic_json(metadata_path, metadata)
    _write_compat_records(runtime, args.service, os.getpid(), instance)

    child_ack_r, child_ack_w = os.pipe()
    child_ready_r, child_ready_w = os.pipe()
    output_r, output_w = os.pipe()
    guard_pid = 0
    guard_control_w: int | None = None
    guard_ready_r: int | None = None
    child_status: int | None = None
    guard_status: int | None = None
    child: dict[str, Any] = {}
    try:
        child_pid = os.fork()
    except BaseException as exc:
        for fd in (child_ack_r, child_ack_w, child_ready_r, child_ready_w, output_r, output_w, args.ready_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _update_metadata(metadata_path, metadata, "failed")
        except Exception:
            pass
        raise LifecycleError("unable to fork service child") from exc
    if child_pid == 0:
        try:
            os.close(child_ack_w)
            os.close(child_ready_r)
            os.close(output_r)
            os.close(args.ready_fd)
            os.dup2(output_w, 1)
            os.dup2(output_w, 2)
            os.close(output_w)
            # Separate process group, same service session as the supervisor.
            os.setpgid(0, 0)
            os.write(child_ready_w, b"P")
            os.close(child_ready_w)
            token = os.read(child_ack_r, 1)
            os.close(child_ack_r)
            if token != b"A":
                os._exit(75)
            os.execvpe(command[0], command, os.environ.copy())
        except BaseException as exc:
            try:
                os.write(child_ready_w, ("E" + str(exc)).encode("utf-8", errors="replace"))
            except OSError:
                pass
            os._exit(127)
    os.close(child_ack_r)
    os.close(child_ready_w)
    os.close(output_w)

    # Fork the independent sentinel immediately, before waiting for metadata
    # or even the child's readiness byte.  A supervisor SIGKILL in any of
    # those pre-ACK windows therefore closes guard_control_w and the guard
    # tears down the service SID rather than leaving a blocked child behind.
    guard_control_r: int | None = None
    guard_control_w_fd: int | None = None
    guard_ready_r_fd: int | None = None
    guard_ready_w: int | None = None
    try:
        guard_control_r, guard_control_w_fd = os.pipe()
        guard_ready_r_fd, guard_ready_w = os.pipe()
    except BaseException as exc:
        for fd in (
            child_ack_w,
            child_ready_r,
            guard_control_r,
            guard_control_w_fd,
            guard_ready_r_fd,
            guard_ready_w,
            output_r,
            args.ready_fd,
        ):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _terminate_session(service_sid, exclude={os.getpid()}, grace=0.5)
        except Exception:
            pass
        _reap_bounded(child_pid, timeout=5)
        try:
            _update_metadata(metadata_path, metadata, "failed")
        except Exception:
            pass
        raise LifecycleError("unable to establish lifecycle handshake") from exc
    guard_control_w = guard_control_w_fd
    guard_ready_r = guard_ready_r_fd

    def close_setup_fds() -> None:
        for fd in (
            child_ack_w,
            child_ready_r,
            guard_control_r,
            guard_control_w,
            guard_ready_r,
            guard_ready_w,
            output_r,
            args.ready_fd,
        ):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

    def abort_setup() -> None:
        """Close every pre-READY window without trusting a recycled PID."""

        if guard_control_w is not None:
            try:
                os.write(guard_control_w, b"S")
            except OSError:
                pass
        try:
            _terminate_session(service_sid, exclude={os.getpid()}, grace=0.5)
        except Exception:
            # The direct child is still ours and cannot have been reaped by a
            # different parent.  If its precise identity is available, give
            # it one last direct TERM; otherwise _reap_bounded will escalate
            # only the known direct-child PID after the bounded window.
            try:
                actual_child = process_identity(child_pid)
                if (
                    actual_child is not None
                    and int(actual_child.get("sid", -1)) == service_sid
                    and int(actual_child.get("pgid", -1)) == child_pid
                ):
                    os.kill(child_pid, signal.SIGTERM)
            except (IdentityUnknown, ProcessLookupError, PermissionError, OSError):
                pass
        _reap_bounded(child_pid, timeout=5)
        if guard_pid > 1:
            try:
                os.kill(guard_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            _reap_bounded(guard_pid, timeout=5)
        close_setup_fds()
        try:
            _update_metadata(metadata_path, metadata, "failed")
        except Exception:
            pass

    try:
        guard_pid = os.fork()
    except BaseException as exc:
        abort_setup()
        raise LifecycleError("unable to establish lifecycle guard") from exc
    if guard_pid == 0:
        try:
            os.close(guard_control_w_fd)
            os.close(guard_ready_r_fd)
            os.close(child_ack_w)
            os.close(args.ready_fd)
            code = _guard(
                service_sid,
                supervisor,
                os.getppid(),
                child_pid,
                guard_control_r,
                guard_ready_w,
                output_r,
                Path(args.log_path),
            )
        except BaseException:
            code = 1
        os._exit(code)
    os.close(guard_control_r)
    os.close(guard_ready_w)
    os.close(output_r)

    try:
        child_ready_token = _read_byte(child_ready_r, DEFAULT_READY_SECONDS)
        if child_ready_token != b"P":
            detail = os.read(child_ready_r, 4096).decode("utf-8", errors="replace")
            raise LifecycleError(f"service child setup failed: {detail}")
        os.close(child_ready_r)
        child = process_identity(child_pid)
        if child is None:
            raise LifecycleError("service child disappeared before handshake")
        if int(child["sid"]) != service_sid:
            raise IdentityMismatch("service child is outside supervisor session")
        child["expected_command"] = command[0]
        _test_gate(runtime, args.service, "pre_metadata")
    except BaseException:
        abort_setup()
        raise
    requested_signal: int | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal requested_signal
        requested_signal = signum

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    try:
        payload = json.loads(_readline(guard_ready_r_fd, DEFAULT_READY_SECONDS).decode())
        if "error" in payload:
            raise LifecycleError(payload["error"])
        guard = payload["guard"]
        _test_gate(runtime, args.service, "ack")
        _update_metadata(metadata_path, metadata, guard=guard, child=child)
        # The guard ACK is intentionally written before the child ACK.  If we
        # die in this tiny window the guard sees EOF and kills the blocked child.
        os.write(guard_control_w, b"A")
        os.write(child_ack_w, b"A")
        os.close(child_ack_w)
        _update_metadata(metadata_path, metadata, "running")
        os.write(args.ready_fd, b"READY\n")
        os.close(args.ready_fd)
        while True:
            if requested_signal is not None:
                if metadata.get("state") != "stopping":
                    _update_metadata(metadata_path, metadata, "stopping", stop_signal=requested_signal)
                try:
                    os.write(guard_control_w, b"S")
                except OSError:
                    pass
            if child_status is None:
                try:
                    waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    waited_pid, status = child_pid, 0
                if waited_pid == child_pid:
                    child_status = _wait_exit_code(status)
                    # A leader can exit while descendants keep the session
                    # alive; ask the guard to clean those descendants.
                    try:
                        os.write(guard_control_w, b"S")
                    except OSError:
                        pass
            if guard_status is None:
                try:
                    waited_pid, status = os.waitpid(guard_pid, os.WNOHANG)
                except ChildProcessError:
                    waited_pid, status = guard_pid, 0
                if waited_pid == guard_pid:
                    guard_status = _wait_exit_code(status)
                    # Whether or not the leader was already reaped, the
                    # independent guard's death means descendants may still
                    # occupy the recorded service SID.  The supervisor is
                    # alive and is the SID owner, so this cleanup is safe.
                    _cleanup_after_guard_exit(metadata, reap_child=False)
                    if child_status is None:
                        # We own this child, so a guard SIGKILL is recoverable:
                        # validate its identity, terminate the service SID and
                        # reap the child before committing terminal metadata.
                        reaped = _reap_bounded(child_pid)
                        child_status = 0 if reaped is None else reaped
                    break
            if child_status is not None and guard_status is not None:
                break
            time.sleep(0.1)
        effective_status = child_status if child_status not in (None, 0) else guard_status
        if effective_status is None:
            effective_status = 0
        _update_metadata(
            metadata_path,
            metadata,
            "exited",
            exit={"status": effective_status, "child": child_status, "guard": guard_status},
        )
        if requested_signal is not None:
            return 128 + requested_signal
        return _shell_exit_code(effective_status)
    except BaseException:
        try:
            os.write(guard_control_w, b"S")
        except OSError:
            pass
        if child_pid > 1 and child_status is None:
            # Session enumeration is intentionally fail-closed and test
            # adapters may not know a pre-ACK child yet; signal the directly
            # owned child after validating its precise birth/PGID/SID.
            try:
                actual_child = process_identity(child_pid)
                expected_child = dict(child)
                expected_child.pop("expected_command", None)
                if actual_child is not None and int(actual_child.get("sid", -1)) == service_sid and (
                    not expected_child or _identity_matches(expected_child, actual_child, require_precise=True)
                ):
                    os.kill(child_pid, signal.SIGTERM)
            except (IdentityUnknown, IdentityMismatch, ProcessLookupError, PermissionError):
                pass
            try:
                _terminate_session(service_sid, exclude={os.getpid()}, grace=0.5)
            except Exception:
                pass
        if child_status is None:
            child_status = _reap_bounded(child_pid)
        if guard_status is None:
            guard_status = _reap_bounded(guard_pid)
        try:
            _update_metadata(metadata_path, metadata, "failed")
        except Exception:
            pass
        raise
    finally:
        for fd in (guard_control_w, guard_ready_r):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass


def _launch(runtime: Path, service: Service, python_bin: Path, instance: str) -> subprocess.Popen[bytes]:
    read_fd, write_fd = os.pipe()
    command = [str(service.script)]
    try:
        process = subprocess.Popen(
            [
                str(python_bin),
                str(Path(__file__).resolve()),
                "supervise",
                "--runtime-dir",
                str(runtime),
                "--service",
                service.name,
                "--instance",
                instance,
                "--port",
                str(service.port),
                "--endpoint",
                service.endpoint,
                "--command-json",
                json.dumps(command),
                "--log-path",
                str(service.log_path),
                "--ready-fd",
                str(write_fd),
            ],
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(write_fd)
    try:
        message = _readline(read_fd, DEFAULT_READY_SECONDS)
    except BaseException:
        _terminate_process_bounded(process)
        raise
    finally:
        os.close(read_fd)
    if message != b"READY":
        _terminate_process_bounded(process)
        raise LifecycleError(f"{service.name} supervisor did not become ready")
    return process


def _legacy_identity(
    runtime: Path,
    service: str,
    *,
    script: Path,
) -> tuple[int, dict[str, Any] | None]:
    pid_path = _pid_file(runtime, service)
    try:
        raw_pid = _read_regular_bytes(
            pid_path,
            maximum=MAX_COMPAT_RECORD_BYTES,
            label=f"{service} legacy PID record",
        )
        pid = int(raw_pid.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise LifecycleError(f"{service} legacy PID record is invalid") from exc
    if pid <= 1 or pid > MAX_PID:
        raise LifecycleError(f"{service} legacy PID record is invalid")
    actual = process_identity(pid)
    # Match the release-local argv token, not the symlink target of the venv
    # interpreter: macOS `ps command` preserves the path used to launch it.
    expected_python = runtime / "venv/bin/python"
    expected_wrapper = (script.parent / "logging_wrapper.py").resolve()
    expected_script = script.resolve()
    if actual is not None:
        if not actual.get("birth_precise"):
            raise IdentityUnknown(f"{service} legacy PID birth is not precise")
        command = str(actual.get("command", ""))
        required = (str(expected_python), str(expected_wrapper), str(expected_script))
        if any(token not in command for token in required):
            raise IdentityMismatch(f"{service} legacy PID is not this release")
    return pid, actual


def _legacy_reconcile(
    runtime: Path,
    service: str,
    *,
    port: int,
    script: Path,
) -> None:
    pid_path = _pid_file(runtime, service)
    if not pid_path.exists():
        return
    pid, actual = _legacy_identity(runtime, service, script=script)
    if actual is not None:
        _signal_exact(actual, signal.SIGTERM, label=f"{service} legacy PID")
        deadline = time.monotonic() + DEFAULT_READY_SECONDS
        while time.monotonic() < deadline:
            current = process_identity(pid)
            if current is None:
                break
            if not _identity_matches(actual, current, require_precise=True):
                raise IdentityMismatch(f"{service} legacy PID changed while stopping")
            time.sleep(0.05)
        else:
            raise LifecycleError(f"{service} legacy PID remains after TERM; evidence retained")
    listeners = _listener_pids(port)
    if listeners:
        raise LifecycleError(f"{service} legacy listener(s) remain: {listeners}; evidence retained")
    _remove_records(runtime, service)


def _reconcile(
    runtime: Path,
    *,
    refuse_live: bool,
    ports: dict[str, int] | None = None,
    scripts: dict[str, Path] | None = None,
) -> None:
    ports = ports or {"backend": 5001, "api": 8000}
    scripts = scripts or {
        service: runtime.parents[2] / "services/docling-service/deploy/macos" / f"run-{service}.sh"
        for service in ("backend", "api")
    }
    for service in ("backend", "api"):
        path = _meta_file(runtime, service)
        if not path.exists():
            _legacy_reconcile(runtime, service, port=ports[service], script=scripts[service])
            continue
        metadata = _read_service_metadata(path, service)
        live = False
        for role, token in (("supervisor", "lifecycle.py"), ("guard", None), ("child", None)):
            item = metadata.get(role)
            if not item:
                continue
            actual = process_identity(int(item["pid"]))
            if actual is None:
                continue
            if not _identity_matches(item, actual, token=token, require_precise=True):
                raise IdentityMismatch(f"{service} {role} identity mismatch")
            if role in {"supervisor", "child"} and int(actual["sid"]) != int(metadata["service_sid"]):
                raise IdentityMismatch(f"{service} {role} service session mismatch")
            if role == "guard" and int(actual["sid"]) == int(metadata["service_sid"]):
                raise IdentityMismatch(f"{service} guard session mismatch")
            live = True
        if live and refuse_live:
            raise LifecycleError(f"{service} is already running")
        if not live:
            # A role PID may be gone while a detached descendant still holds
            # the service SID.  Do not delete the only recovery record in that
            # case; a recycled SID is equally unsafe to guess at.
            members = _session_members(int(metadata["service_sid"]), exclude=())
            if members:
                raise LifecycleError(f"{service} has untracked service-session members")
            port = metadata.get("port")
            if isinstance(port, int) and _listener_pids(port):
                raise LifecycleError(f"{service} listener remains outside recorded service session")
            _remove_records(runtime, service)


def _stop_one(runtime: Path, service: str, *, port: int, script: Path) -> None:
    path = _meta_file(runtime, service)
    if not path.exists():
        if _pid_file(runtime, service).exists():
            _legacy_reconcile(runtime, service, port=port, script=script)
        else:
            # An instance file without its PID carries no process authority.
            _remove_records(runtime, service)
        return
    metadata = _read_service_metadata(path, service)

    def inspect() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        current = _read_service_metadata(path, service)
        if current["instance"] != metadata["instance"]:
            raise IdentityMismatch(f"{service} instance changed while stopping")
        live: dict[str, dict[str, Any]] = {}
        for role in ("supervisor", "guard", "child"):
            item = current.get(role)
            if not isinstance(item, dict):
                continue
            actual = process_identity(int(item["pid"]))
            if actual is None:
                continue
            if not _identity_matches(
                item,
                actual,
                token="lifecycle.py" if role == "supervisor" else None,
                require_precise=True,
            ):
                raise IdentityMismatch(f"{service} {role} identity mismatch")
            if role in {"supervisor", "child"} and int(actual["sid"]) != int(current["service_sid"]):
                raise IdentityMismatch(f"{service} {role} service session mismatch")
            if role == "guard" and int(actual["sid"]) == int(current["service_sid"]):
                raise IdentityMismatch(f"{service} guard service session mismatch")
            live[role] = actual
        return current, live

    def finalize_if_dead(current: dict[str, Any], live: dict[str, dict[str, Any]]) -> bool:
        if live:
            return False
        members = _session_members(int(current["service_sid"]), exclude=())
        if members:
            raise LifecycleError(f"{service} has untracked service-session members")
        current_port = current.get("port")
        if isinstance(current_port, int) and _listener_pids(current_port):
            raise LifecycleError(f"{service} listener remains outside recorded service session")
        _remove_records(runtime, service)
        return True

    current, live = inspect()
    if finalize_if_dead(current, live):
        return
    if "supervisor" in live:
        target_role = "supervisor"
    elif "guard" in live:
        target_role = "guard"
    else:
        target_role = "child"
    if target_role == "child":
        _terminate_session(int(current["service_sid"]), exclude=(), grace=0.5)
    else:
        _signal_exact(
            current[target_role],
            signal.SIGTERM,
            label=f"{service} {target_role}",
            token="lifecycle.py" if target_role == "supervisor" else None,
            service_sid=int(current["service_sid"]),
            outside_service_sid=target_role == "guard",
        )

    grace_deadline = time.monotonic() + DEFAULT_GRACE_SECONDS
    while time.monotonic() < grace_deadline:
        current, live = inspect()
        if finalize_if_dead(current, live):
            return
        time.sleep(0.05)

    # A stopped or wedged supervisor/guard cannot run its signal handler.
    # After the graceful window, exact birth/role/session validation above
    # authorizes a bounded hard cleanup without ever guessing at a PID.
    current, live = inspect()
    if "supervisor" in live or "child" in live:
        _terminate_session(int(current["service_sid"]), exclude=(), grace=0.5)
    guard = live.get("guard")
    guard_item = current.get("guard")
    if guard is not None and isinstance(guard_item, dict):
        _signal_exact(
            guard_item,
            signal.SIGKILL,
            label=f"{service} guard",
            service_sid=int(current["service_sid"]),
            outside_service_sid=True,
        )

    deadline = time.monotonic() + DEFAULT_READY_SECONDS
    while time.monotonic() < deadline:
        current, live = inspect()
        if finalize_if_dead(current, live):
            return
        time.sleep(0.05)
    raise LifecycleError(f"unable to stop {service}; metadata retained")


def _start_all(args: argparse.Namespace) -> int:
    runtime = Path(args.runtime_dir).resolve()
    python_bin = Path(args.python_bin).resolve()
    services = [
        Service("backend", Path(args.backend_script).resolve(), args.backend_port, f"http://127.0.0.1:{args.backend_port}/version", runtime / "logs/backend.log"),
        Service("api", Path(args.api_script).resolve(), args.api_port, f"http://127.0.0.1:{args.api_port}/healthz", runtime / "logs/api.log"),
    ]
    started: list[subprocess.Popen[bytes]] = []
    signal_received: int | None = None

    def capture_signal(signum: int, _frame: Any) -> None:
        nonlocal signal_received
        signal_received = signum

    old_handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    for signum in old_handlers:
        signal.signal(signum, capture_signal)
    try:
        with LifecycleLock(runtime / "pids/.lifecycle.lock"):
            _reconcile(
                runtime,
                refuse_live=True,
                ports={service.name: service.port for service in services},
                scripts={service.name: service.script for service in services},
            )
            for service in services:
                if _listener_pids(service.port):
                    raise LifecycleError(f"port {service.port} already has a listener")
                instance = secrets.token_hex(16)
                process = _launch(runtime, service, python_bin, instance)
                started.append(process)
                for _ in range(_env_positive(f"DOCLING_MACOS_{service.name.upper()}_READY_ATTEMPTS", 120 if service.name == "backend" else 30)):
                    if signal_received is not None:
                        raise LifecycleError("startup interrupted")
                    if process.poll() is not None:
                        raise LifecycleError(
                            f"{service.name} supervisor exited with status {process.returncode}"
                        )
                    if _health_owned(runtime, service):
                        break
                    time.sleep(1)
                else:
                    raise LifecycleError(f"{service.name} did not become ready")
            print(f"docling-service is ready at http://127.0.0.1:{args.api_port}")
            return 0
    except Exception as exc:
        for process in reversed(started):
            _terminate_process_bounded(process, timeout=DEFAULT_READY_SECONDS)
        if signal_received is not None:
            return 128 + signal_received
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def _stop_all(args: argparse.Namespace) -> int:
    runtime = Path(args.runtime_dir).resolve()
    try:
        with LifecycleLock(runtime / "pids/.lifecycle.lock"):
            script_dir = Path(__file__).resolve().parent
            for service in ("api", "backend"):
                port, _endpoint = _default_service_endpoint(service)
                _stop_one(
                    runtime,
                    service,
                    port=port,
                    script=script_dir / f"run-{service}.sh",
                )
        print("docling-service stopped")
        return 0
    except (LifecycleError, BusyError, IdentityUnknown, IdentityMismatch) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _status(args: argparse.Namespace) -> int:
    runtime = Path(args.runtime_dir).resolve()
    statuses: list[dict[str, Any]] = []
    for service in ("backend", "api"):
        path = _meta_file(runtime, service)
        if not path.exists():
            pid_path = _pid_file(runtime, service)
            if pid_path.exists():
                try:
                    script = Path(__file__).resolve().parent / f"run-{service}.sh"
                    pid, actual = _legacy_identity(runtime, service, script=script)
                    state = "legacy-running" if actual is not None else "legacy-stale"
                    statuses.append({"service": service, "state": state, "pid": pid})
                except (LifecycleError, OSError, ValueError) as exc:
                    statuses.append({"service": service, "state": "unknown", "error": str(exc)})
            else:
                try:
                    port, _endpoint = _default_service_endpoint(service)
                    state = "unknown" if _listener_pids(port) else "stopped"
                    statuses.append({"service": service, "state": state})
                except LifecycleError as exc:
                    statuses.append({"service": service, "state": "unknown", "error": str(exc)})
            continue
        try:
            metadata = _read_service_metadata(path, service)
            live_roles = 0
            identity_error = False
            live_by_role: dict[str, bool] = {}
            for role in ("supervisor", "guard", "child"):
                item = metadata.get(role)
                if not item:
                    continue
                actual = process_identity(int(item["pid"]))
                if actual is None:
                    live_by_role[role] = False
                    continue
                if not _identity_matches(item, actual, token="lifecycle.py" if role == "supervisor" else None, require_precise=True):
                    identity_error = True
                    continue
                if role in {"supervisor", "child"} and int(actual["sid"]) != int(metadata["service_sid"]):
                    identity_error = True
                    continue
                if role == "guard" and int(actual["sid"]) == int(metadata["service_sid"]):
                    identity_error = True
                    continue
                live_roles += 1
                live_by_role[role] = True
            health_ok: bool | None = None
            if metadata["state"] == "running" and live_by_role.get("supervisor") and live_by_role.get("child"):
                try:
                    port = metadata.get("port")
                    endpoint = metadata.get("endpoint")
                    if isinstance(port, int) and isinstance(endpoint, str):
                        health_ok = _health_owned(
                            runtime,
                            Service(service, Path("."), port, endpoint, runtime / f"logs/{service}.log"),
                        )
                    else:
                        health_ok = False
                except (LifecycleError, ValueError):
                    health_ok = False
            untracked_members = False
            detached_listener = False
            if live_roles == 0:
                try:
                    untracked_members = bool(_session_members(int(metadata["service_sid"]), exclude=()))
                    port = metadata.get("port")
                    if isinstance(port, int):
                        detached_listener = bool(_listener_pids(port))
                except LifecycleError:
                    identity_error = True
            orphaned_roles = live_roles > 0 and not live_by_role.get("supervisor", False)
            missing_child = (
                metadata["state"] in {"running", "stopping"}
                and live_by_role.get("supervisor", False)
                and not live_by_role.get("child", False)
            )
            unhealthy = metadata["state"] == "running" and health_ok is False
            state = "unknown" if identity_error or orphaned_roles or missing_child or unhealthy or untracked_members or detached_listener else (metadata["state"] if live_roles else "stale")
            entry = {"service": service, "state": state, "instance": metadata["instance"]}
            if health_ok is not None:
                entry["health"] = "ok" if health_ok else "unhealthy"
            if detached_listener:
                entry["listener"] = "untracked"
            statuses.append(entry)
        except LifecycleError as exc:
            statuses.append({"service": service, "state": "unknown", "error": str(exc)})
    print(json.dumps(statuses, sort_keys=True))
    healthy = len(statuses) == 2 and all(
        item.get("state") == "running" and item.get("health") == "ok" for item in statuses
    )
    return 0 if healthy else 1


def _legacy(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        raise LifecycleError("missing command")
    if args.max_bytes is not None:
        os.environ["DOCLING_MACOS_LOG_MAX_BYTES"] = str(args.max_bytes)
    if args.backup_count is not None:
        os.environ["DOCLING_MACOS_LOG_BACKUP_COUNT"] = str(args.backup_count)
    writer = LogWriter(Path(args.log_path).expanduser())
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=True,
    )
    termination_signal: int | None = None
    force_kill_started = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal termination_signal
        termination_signal = signum
        if child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            return
        except OSError:
            return
        if force_kill_started.is_set():
            return
        force_kill_started.set()

        def force_kill() -> None:
            time.sleep(DEFAULT_GRACE_SECONDS)
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    pass

        threading.Thread(target=force_kill, daemon=True, name="docling-legacy-log-kill").start()

    old_handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    for signum in old_handlers:
        signal.signal(signum, request_shutdown)
    try:
        assert child.stdout is not None
        while True:
            chunk = child.stdout.read(65536)
            if not chunk:
                break
            writer.write(chunk)
        code = child.wait()
        return (128 + termination_signal) if termination_signal is not None else code
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        writer.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Docling macOS lifecycle helper")
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start-all")
    start.add_argument("--runtime-dir", required=True)
    start.add_argument("--python-bin", required=True)
    start.add_argument("--backend-script", required=True)
    start.add_argument("--api-script", required=True)
    start.add_argument("--backend-port", type=_port, default=5001)
    start.add_argument("--api-port", type=_port, default=8000)
    stop = sub.add_parser("stop-all")
    stop.add_argument("--runtime-dir", required=True)
    status = sub.add_parser("status")
    status.add_argument("--runtime-dir", required=True)
    supervise = sub.add_parser("supervise", help=argparse.SUPPRESS)
    supervise.add_argument("--runtime-dir", required=True)
    supervise.add_argument("--service", choices=("backend", "api"), required=True)
    supervise.add_argument("--instance", required=True)
    supervise.add_argument("--port", type=_port, default=None)
    supervise.add_argument("--endpoint", default=None)
    supervise.add_argument("--command-json", required=True)
    supervise.add_argument("--log-path", required=True)
    supervise.add_argument("--ready-fd", type=int, required=True)
    legacy = sub.add_parser("legacy", help=argparse.SUPPRESS)
    legacy.add_argument("--log-path", required=True)
    legacy.add_argument("--max-bytes", type=_log_size, default=None)
    legacy.add_argument("--backup-count", type=_backup_count, default=None)
    legacy.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "start-all":
        return _start_all(args)
    if args.action == "stop-all":
        return _stop_all(args)
    if args.action == "status":
        return _status(args)
    if args.action == "supervise":
        return _supervise(args)
    if args.action == "legacy":
        return _legacy(args)
    raise LifecycleError(f"unknown lifecycle action: {args.action}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (LifecycleError, BusyError, IdentityUnknown, IdentityMismatch) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
