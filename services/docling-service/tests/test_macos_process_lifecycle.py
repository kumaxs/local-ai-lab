from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock


SERVICE_ROOT = Path(__file__).resolve().parents[1]
MACOS_DEPLOY = SERVICE_ROOT / "deploy/macos"
LIFECYCLE = MACOS_DEPLOY / "lifecycle.py"
_PORT_COUNTER = 20000


def _wait_for(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    helper = os.environ.get("FAKE_PS_HELPER")
    if helper:
        try:
            result = subprocess.run(
                [helper, str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None:
            if result.returncode != 0:
                return False
            state = result.stdout.strip()
            if not state or state.startswith("Z"):
                return False
            return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _free_port() -> int:
    global _PORT_COUNTER
    _PORT_COUNTER += 1
    return _PORT_COUNTER


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class MacOSProcessLifecycleTests(unittest.TestCase):
    maxDiff = None

    def _load_lifecycle(self):
        module_name = f"docling_macos_lifecycle_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(module_name, LIFECYCLE)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _write_tool(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _write_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        runtime = root / ".runtime/docling-release/macos"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "logs").mkdir(parents=True, exist_ok=True)
        (runtime / "pids").mkdir(parents=True, exist_ok=True)
        registry = root / "registry"
        registry.mkdir()

        service_py = root / "fake_service.py"
        service_py.write_text(
            textwrap.dedent(
                f"""
                import json
                import os
                import signal
                import subprocess
                import sys
                import time
                from pathlib import Path

                role = sys.argv[1]
                registry = Path(os.environ["FAKE_REG_DIR"])
                registry.mkdir(parents=True, exist_ok=True)
                (registry / f"{{role}}.pid").write_text(str(os.getpid()), encoding="utf-8")

                def spawn_descendant(tag: str) -> int:
                    marker = registry / f"{{role}}.{{tag}}.pid"
                    code = (
                        "from pathlib import Path; "
                        "import atexit, os, signal, sys, time; "
                        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
                        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
                        "signal.signal(signal.SIGHUP, lambda *_: sys.exit(0)); "
                        "marker = Path(sys.argv[1]); "
                        "atexit.register(lambda: marker.unlink(missing_ok=True)); "
                        "time.sleep(30)"
                    )
                    proc = subprocess.Popen([sys.executable, "-c", code, str(marker)])
                    return int(proc.pid)

                mode = os.environ.get(f"FAKE_{{role.upper()}}_MODE", "serve")
                if os.environ.get(f"FAKE_{{role.upper()}}_DESCENDANT") == "1":
                    spawn_descendant("descendant")
                if mode == "spawn-descendant-exit":
                    descendant_pid = spawn_descendant("descendant")
                    marker = registry / f"{{role}}.descendant.pid"
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and not marker.exists():
                        time.sleep(0.05)
                    (registry / f"{{role}}.descendant.spawned.pid").write_text(
                        str(descendant_pid),
                        encoding="utf-8",
                    )
                    sys.exit(0)
                if mode == "exit-nonzero":
                    sys.exit(7)
                gate = os.environ.get(f"FAKE_{{role.upper()}}_GATE")
                if gate:
                    gate_path = Path(gate)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not gate_path.exists():
                        time.sleep(0.05)
                port = int(os.environ.get("DOCLING_BACKEND_PORT", "5001") if role == "backend" else os.environ.get("DOCLING_API_PORT", "8000"))
                (registry / f"{{role}}.listen.json").write_text(
                    json.dumps({{"pid": os.getpid(), "port": port}}),
                    encoding="utf-8",
                )
                running = True

                def request_stop(_signum, _frame):
                    global running
                    running = False

                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    signal.signal(signum, request_stop)

                try:
                    while running:
                        time.sleep(0.05)
                finally:
                    try:
                        (registry / f"{{role}}.pid").unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        (registry / f"{{role}}.listen.json").unlink()
                    except FileNotFoundError:
                        pass
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        backend_script = self._write_tool(
            root / "run-backend.sh",
            f"#!/bin/sh\nexec {sys.executable} {service_py} backend\n",
        )
        api_script = self._write_tool(
            root / "run-api.sh",
            f"#!/bin/sh\nexec {sys.executable} {service_py} api\n",
        )
        ps_helper = self._write_tool(
            root / "fake_ps_state.py",
            textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                pid = int(sys.argv[1])
                registry = Path(os.environ["FAKE_REG_DIR"])
                runtime = Path(os.environ["FAKE_RUNTIME_DIR"])

                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError, OSError):
                    raise SystemExit(1)

                for path in registry.glob("*.pid"):
                    try:
                        if int(path.read_text(encoding="utf-8").strip()) == pid:
                            print("S")
                            raise SystemExit(0)
                    except Exception:
                        continue
                for path in registry.glob("*.listen.json"):
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if int(payload["pid"]) == pid:
                            print("S")
                            raise SystemExit(0)
                    except Exception:
                        continue
                for path in (runtime / "pids").glob("*.meta.json"):
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    for role in ("supervisor", "guard", "child"):
                        item = payload.get(role)
                        if isinstance(item, dict) and int(item.get("pid", -1)) == pid:
                            print("S")
                            raise SystemExit(0)
                print("Z")
                """
            ).strip()
            + "\n",
        )

        helper_py = root / "lifecycle_helper.py"
        helper_py.write_text(
            textwrap.dedent(
                f"""
                import importlib.util
                import json
                import os
                import subprocess
                import sys
                import time
                from pathlib import Path

                lifecycle_path = Path({str(LIFECYCLE)!r})
                helper_path = Path(__file__).resolve()

                spec = importlib.util.spec_from_file_location("docling_test_lifecycle_runtime", lifecycle_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)

                runtime = Path(os.environ["FAKE_RUNTIME_DIR"])
                registry = Path(os.environ["FAKE_REG_DIR"])

                def alive(pid: int) -> bool:
                    if pid <= 1:
                        return False
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return False
                    except PermissionError:
                        return False
                    except OSError:
                        return False
                    return True

                def patched_process_identity(pid: int):
                    if not alive(pid):
                        return None
                    try:
                        pgid = os.getpgid(pid)
                        sid = os.getsid(pid)
                    except OSError:
                        return None
                    command = "python lifecycle.py"
                    birth = f"birth-{{pid}}"
                    for path in (runtime / "pids").glob("*.meta.json"):
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        for role in ("supervisor", "guard", "child"):
                            item = payload.get(role)
                            if isinstance(item, dict) and int(item.get("pid", -1)) == pid:
                                command = str(item.get("command", command))
                                birth = str(item.get("birth", birth))
                                break
                    return {{
                        "pid": pid,
                        "birth": birth,
                        "birth_precise": True,
                        "lstart": f"lstart-{{pid}}",
                        "pgid": int(pgid),
                        "sid": int(sid),
                        "command": command,
                        "state": "S",
                    }}

                def iter_known_pids():
                    seen = set()
                    for path in registry.glob("*.pid"):
                        try:
                            pid = int(path.read_text(encoding="utf-8").strip())
                        except Exception:
                            continue
                        seen.add(pid)
                    for path in registry.glob("*.listen.json"):
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                            seen.add(int(payload["pid"]))
                        except Exception:
                            continue
                    for path in (runtime / "pids").glob("*.meta.json"):
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        for role in ("supervisor", "guard", "child"):
                            item = payload.get(role)
                            if isinstance(item, dict) and isinstance(item.get("pid"), int):
                                seen.add(int(item["pid"]))
                    return sorted(seen)

                def patched_session_members(sid: int, *, exclude=()):
                    excluded = set(exclude)
                    members = []
                    for pid in iter_known_pids():
                        if pid in excluded or not alive(pid):
                            continue
                        try:
                            if os.getsid(pid) == sid:
                                members.append(pid)
                        except OSError:
                            continue
                    return sorted(set(members))

                def patched_listener_pids(port: int):
                    pids = []
                    for path in registry.glob("*.listen.json"):
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                            if int(payload["port"]) == int(port) and alive(int(payload["pid"])):
                                pids.append(int(payload["pid"]))
                        except Exception:
                            continue
                    return sorted(set(pids))

                def patched_health(endpoint: str, timeout: int, *, curl: str = "curl") -> bool:
                    fail_substr = os.environ.get("FAKE_HEALTH_FAIL_SUBSTR", "")
                    if fail_substr and fail_substr in endpoint:
                        return False
                    role = "backend" if endpoint.endswith("/version") else "api"
                    return (registry / f"{{role}}.listen.json").exists()

                real_atomic = module._atomic_json
                def patched_atomic(path, payload):
                    fail_seq = int(os.environ.get("FAKE_FAIL_ATOMIC_SEQ", "0") or "0")
                    if fail_seq and int(payload.get("seq", 0)) >= fail_seq:
                        raise OSError("forced atomic failure")
                    return real_atomic(path, payload)

                real_update = module._update_metadata
                def patched_update(path, metadata, state=None, **extra):
                    pause_path = os.environ.get("FAKE_PAUSE_BEFORE_ACK")
                    release_path = os.environ.get("FAKE_RELEASE_BEFORE_ACK")
                    if pause_path and "guard" in extra and "child" in extra and not Path(pause_path).exists():
                        payload = {{
                            "guard_pid": extra["guard"]["pid"],
                            "child_pid": extra["child"]["pid"],
                        }}
                        Path(pause_path).write_text(json.dumps(payload), encoding="utf-8")
                        if release_path:
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline and not Path(release_path).exists():
                                time.sleep(0.05)
                    return real_update(path, metadata, state, **extra)

                def patched_launch(runtime_dir, service, python_bin, instance):
                    read_fd, write_fd = os.pipe()
                    proc = subprocess.Popen(
                        [
                            str(python_bin),
                            str(helper_path),
                            "supervise-patched",
                            "--runtime-dir",
                            str(runtime_dir),
                            "--service",
                            service.name,
                            "--instance",
                            instance,
                            "--command-json",
                            json.dumps([str(service.script)]),
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
                        env=os.environ.copy(),
                    )
                    os.close(write_fd)
                    try:
                        message = module._readline(read_fd, 10)
                    finally:
                        os.close(read_fd)
                    if message != b"READY":
                        proc.terminate()
                        proc.wait(timeout=5)
                        raise RuntimeError(f"{{service.name}} supervisor did not become ready")
                    return proc

                module.process_identity = patched_process_identity
                module._session_members = patched_session_members
                module._listener_pids = patched_listener_pids
                module._health = patched_health
                module._atomic_json = patched_atomic
                module._update_metadata = patched_update

                mode = sys.argv[1]
                argv = sys.argv[2:]
                if mode == "start-all-patched":
                    module._launch = patched_launch
                    argv = ["start-all", *argv]
                elif mode == "stop-all-patched":
                    argv = ["stop-all", *argv]
                elif mode == "status-patched":
                    argv = ["status", *argv]
                elif mode == "supervise-patched":
                    argv = ["supervise", *argv]
                elif mode == "legacy-patched":
                    argv = ["legacy", *argv]
                else:
                    raise SystemExit(f"unknown helper mode: {{mode}}")
                raise SystemExit(module.main(argv))
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        os.environ["FAKE_PS_HELPER"] = str(ps_helper)
        self.addCleanup(os.environ.pop, "FAKE_PS_HELPER", None)
        return runtime, registry, helper_py, backend_script

    def _helper_env(self, runtime: Path, registry: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        environment["FAKE_RUNTIME_DIR"] = str(runtime)
        environment["FAKE_REG_DIR"] = str(registry)
        if extra:
            environment.update(extra)
        return environment

    def _spawn_helper(self, helper: Path, mode: str, args: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, str(helper), mode, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _spawn_supervisor(
        self,
        helper: Path,
        runtime: Path,
        registry: Path,
        service_script: Path,
        *,
        env_extra: dict[str, str] | None = None,
        instance: str = "supervise-test",
    ) -> tuple[subprocess.Popen[bytes], int]:
        ready_r, ready_w = os.pipe()
        args = [
            "--runtime-dir",
            str(runtime),
            "--service",
            "backend",
            "--instance",
            instance,
            "--command-json",
            json.dumps([str(service_script)]),
            "--log-path",
            str(runtime / "logs/backend.log"),
            "--ready-fd",
            str(ready_w),
        ]
        process = subprocess.Popen(
            [sys.executable, str(helper), "supervise-patched", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._helper_env(runtime, registry, env_extra),
            pass_fds=(ready_w,),
        )
        os.close(ready_w)
        return process, ready_r

    def _terminate_process(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def _read_output(self, process: subprocess.Popen[bytes]) -> tuple[str, str]:
        stdout, stderr = process.communicate(timeout=5)
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    def test_lifecycle_lock_blocks_parallel_holder_and_recovers_after_sigkill(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / ".runtime/docling-release/macos/pids/.lifecycle.lock"
            holder = self._write_tool(
                root / "lock_holder.py",
                textwrap.dedent(
                    f"""
                    import importlib.util
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    spec = importlib.util.spec_from_file_location("docling_lock_holder", {str(LIFECYCLE)!r})
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    with module.LifecycleLock(Path(sys.argv[1])):
                        Path(sys.argv[2]).write_text("ready", encoding="utf-8")
                        time.sleep(30)
                    """
                ).strip()
                + "\n",
            )
            ready = root / "ready.txt"
            process = subprocess.Popen([sys.executable, str(holder), str(lock_path), str(ready)])
            try:
                _wait_for(ready.exists)
                with self.assertRaises(module.BusyError):
                    with module.LifecycleLock(lock_path):
                        pass
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                with module.LifecycleLock(lock_path):
                    self.assertTrue(lock_path.exists())
            finally:
                self._terminate_process(process)

    def test_lifecycle_lock_refuses_symlink(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.lock"
            target.write_text("do-not-touch\n", encoding="utf-8")
            lock_path = root / "pids/.lifecycle.lock"
            lock_path.parent.mkdir()
            lock_path.symlink_to(target)
            with self.assertRaises(module.LifecycleError):
                with module.LifecycleLock(lock_path):
                    pass
            self.assertEqual("do-not-touch\n", target.read_text(encoding="utf-8"))

            lock_path.unlink()
            os.link(target, lock_path)
            with self.assertRaises(module.LifecycleError):
                with module.LifecycleLock(lock_path):
                    pass

    def test_compat_record_atomic_text_is_private_and_cleans_failed_temp(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record"
            module._atomic_text(path, "value\n")
            self.assertEqual("value\n", path.read_text(encoding="utf-8"))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with mock.patch.object(module.os, "replace", side_effect=OSError("forced replace failure")):
                with self.assertRaises(OSError):
                    module._atomic_text(path, "next\n")
            self.assertEqual([], list(path.parent.glob(".record.*.tmp")))
            with mock.patch.object(module.os, "fsync", side_effect=OSError("forced fsync failure")):
                with self.assertRaises(OSError):
                    module._atomic_text(path, "next\n")
            self.assertEqual("value\n", path.read_text(encoding="utf-8"))
            self.assertEqual([], list(path.parent.glob(".record.*.tmp")))

    def test_guard_cleans_service_when_supervisor_pid_identity_is_reused(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control_r, control_w = os.pipe()
            ready_r, ready_w = os.pipe()
            output_r, output_w = os.pipe()
            os.write(control_w, b"A")
            os.close(control_w)
            os.close(output_w)
            supervisor_expected = {
                "pid": 201,
                "birth": "original-supervisor",
                "birth_precise": True,
                "pgid": 201,
                "sid": 401,
                "command": "python lifecycle.py",
            }
            child_identity = {
                "pid": 203,
                "birth": "service-child",
                "birth_precise": True,
                "pgid": 203,
                "sid": 401,
                "command": "service",
            }

            def fake_identity(pid: int):
                if pid == os.getpid():
                    return {
                        "pid": pid,
                        "birth": "guard",
                        "birth_precise": True,
                        "pgid": pid,
                        "sid": 501,
                        "command": "guard",
                    }
                if pid == 203:
                    return dict(child_identity)
                if pid == 201:
                    return {
                        **supervisor_expected,
                        "birth": "reused-pid",
                        "sid": 999,
                    }
                return None

            try:
                with (
                    mock.patch.object(module.os, "setsid"),
                    mock.patch.object(module.os, "getsid", return_value=501),
                    mock.patch.object(module, "process_identity", side_effect=fake_identity),
                    mock.patch.object(module, "_terminate_session") as terminate,
                ):
                    result = module._guard(
                        401,
                        supervisor_expected,
                        201,
                        203,
                        control_r,
                        ready_w,
                        output_r,
                        root / "guard.log",
                    )
                self.assertEqual(0, result)
                terminate.assert_called_once_with(
                    401,
                    exclude=set(),
                    grace=module.DEFAULT_GRACE_SECONDS,
                )
            finally:
                os.close(ready_r)

    def test_supervisor_kill_before_ack_leaves_no_guard_or_service(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            pause_path = root / "pause.json"
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={
                    "DOCLING_BACKEND_PORT": str(_free_port()),
                    "FAKE_PAUSE_BEFORE_ACK": str(pause_path),
                },
            )
            try:
                _wait_for(pause_path.exists)
                paused = _read_json(pause_path)
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                _wait_for(lambda: not _pid_alive(int(paused["guard_pid"])))
                _wait_for(lambda: not _pid_alive(int(paused["child_pid"])))
                self.assertFalse((registry / "backend.listen.json").exists())
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_supervisor_sigkill_after_ack_cleans_guard_and_service(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={"DOCLING_BACKEND_PORT": str(_free_port())},
            )
            metadata_path = runtime / "pids/backend.meta.json"
            try:
                self.assertEqual(b"READY\n", os.read(ready_r, 64))
                _wait_for(lambda: metadata_path.exists() and _read_json(metadata_path)["state"] == "running")
                metadata = _read_json(metadata_path)
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                _wait_for(lambda: not _pid_alive(int(metadata["guard"]["pid"])))
                _wait_for(lambda: not _pid_alive(int(metadata["child"]["pid"])))
                self.assertFalse((registry / "backend.listen.json").exists())
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_guard_sigkill_after_ack_makes_supervisor_clean_descendants(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={
                    "DOCLING_BACKEND_PORT": str(_free_port()),
                    "FAKE_BACKEND_DESCENDANT": "1",
                },
            )
            metadata_path = runtime / "pids/backend.meta.json"
            descendant_path = registry / "backend.descendant.pid"
            try:
                self.assertEqual(b"READY\n", os.read(ready_r, 64))
                _wait_for(lambda: metadata_path.exists() and _read_json(metadata_path)["state"] == "running")
                _wait_for(descendant_path.exists)
                metadata = _read_json(metadata_path)
                descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
                os.kill(int(metadata["guard"]["pid"]), signal.SIGKILL)
                process.wait(timeout=8)
                _wait_for(lambda: not (registry / "backend.listen.json").exists())
                _wait_for(lambda: not descendant_path.exists())
                final_metadata = _read_json(metadata_path)
                self.assertEqual("exited", final_metadata["state"])
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_child_exit_cleans_descendant_and_supervisor_exits(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={"FAKE_BACKEND_MODE": "spawn-descendant-exit"},
            )
            metadata_path = runtime / "pids/backend.meta.json"
            descendant_path = registry / "backend.descendant.pid"
            spawned_path = registry / "backend.descendant.spawned.pid"
            try:
                self.assertEqual(b"READY\n", os.read(ready_r, 64))
                _wait_for(spawned_path.exists)
                descendant_pid = int(spawned_path.read_text(encoding="utf-8"))
                process.wait(timeout=8)
                _wait_for(lambda: not descendant_path.exists())
                self.assertFalse(_pid_alive(descendant_pid))
                self.assertEqual("exited", _read_json(metadata_path)["state"])
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_natural_child_failure_is_normalized_and_propagated(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={"FAKE_BACKEND_MODE": "exit-nonzero"},
            )
            metadata_path = runtime / "pids/backend.meta.json"
            try:
                self.assertEqual(b"READY\n", os.read(ready_r, 64))
                process.wait(timeout=8)
                self.assertEqual(7, process.returncode)
                metadata = _read_json(metadata_path)
                self.assertEqual("exited", metadata["state"])
                self.assertEqual(7, metadata["exit"]["status"])
                self.assertEqual(7, metadata["exit"]["child"])
                self.assertEqual(0, metadata["exit"]["guard"])
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_child_sigkill_cleans_descendant_and_supervisor_exits(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={
                    "DOCLING_BACKEND_PORT": str(_free_port()),
                    "FAKE_BACKEND_DESCENDANT": "1",
                },
            )
            metadata_path = runtime / "pids/backend.meta.json"
            listener_path = registry / "backend.listen.json"
            descendant_path = registry / "backend.descendant.pid"
            try:
                self.assertEqual(b"READY\n", os.read(ready_r, 64))
                _wait_for(lambda: metadata_path.exists() and _read_json(metadata_path)["state"] == "running")
                _wait_for(descendant_path.exists)
                descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
                metadata = _read_json(metadata_path)
                listener_pid = int(_read_json(listener_path)["pid"])
                os.kill(int(metadata["child"]["pid"]), signal.SIGKILL)
                process.wait(timeout=8)
                _wait_for(lambda: not _pid_alive(listener_pid))
                _wait_for(lambda: not _pid_alive(descendant_pid))
                self.assertEqual("exited", _read_json(metadata_path)["state"])
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_supervise_atomic_failure_before_ack_leaves_no_service_listener(self) -> None:
        process = None
        ready_r = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            process, ready_r = self._spawn_supervisor(
                helper,
                runtime,
                registry,
                backend_script,
                env_extra={
                    "DOCLING_BACKEND_PORT": str(_free_port()),
                    "FAKE_FAIL_ATOMIC_SEQ": "2",
                },
            )
            try:
                stdout, stderr = self._read_output(process)
                self.assertNotEqual(0, process.returncode)
                self.assertEqual("", stdout)
                self.assertIn("forced atomic failure", stderr)
                self.assertFalse((registry / "backend.listen.json").exists())
            finally:
                if ready_r is not None:
                    os.close(ready_r)
                self._terminate_process(process)

    def test_stop_all_retains_records_on_malformed_and_identity_mismatch(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            malformed = pids / "backend.meta.json"
            malformed.write_text("{not-json}\n", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                result = module._stop_all(argparse.Namespace(runtime_dir=str(runtime)))
            self.assertEqual(2, result)
            self.assertTrue(malformed.exists())
            self.assertIn("invalid metadata", stderr.getvalue())

        for label, mutated in (
            ("birth", {"birth": "other"}),
            ("pgid", {"pgid": 999}),
            ("sid", {"sid": 888}),
            ("token", {"command": "python not-lifecycle"}),
        ):
            with self.subTest(field=label), tempfile.TemporaryDirectory() as temporary:
                runtime = Path(temporary)
                pids = runtime / "pids"
                pids.mkdir(parents=True)
                metadata_path = pids / "backend.meta.json"
                metadata = {
                    "version": module.METADATA_VERSION,
                    "service": "backend",
                    "instance": "instance-1",
                    "port": 5001,
                    "endpoint": "http://127.0.0.1:5001/version",
                    "service_sid": 401,
                    "state": "running",
                    "seq": 2,
                    "updated_at": 1,
                    "supervisor": {"pid": 201, "birth": "birth-201", "pgid": 301, "sid": 401, "command": "python lifecycle.py"},
                    "guard": {"pid": 202, "birth": "birth-202", "pgid": 302, "sid": 402, "command": "guard"},
                    "child": {"pid": 203, "birth": "birth-203", "pgid": 303, "sid": 403, "command": "child"},
                    "exit": None,
                }
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                def fake_identity(pid: int):
                    if pid == 201:
                        actual = dict(metadata["supervisor"])
                        actual["pid"] = 201
                        actual["birth_precise"] = True
                        actual["lstart"] = "lstart"
                        actual["state"] = "S"
                        actual.update(mutated)
                        return actual
                    return None

                stderr = io.StringIO()
                with mock.patch.object(module, "process_identity", side_effect=fake_identity), mock.patch.object(
                    module.os,
                    "kill",
                    side_effect=AssertionError("stop-all should not signal mismatched identity"),
                ), mock.patch("sys.stderr", stderr):
                    result = module._stop_all(argparse.Namespace(runtime_dir=str(runtime)))
                self.assertEqual(2, result)
                self.assertTrue(metadata_path.exists())
                self.assertIn("identity mismatch", stderr.getvalue())

    def test_legacy_stop_validates_process_and_retains_mismatched_evidence(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / ".runtime/docling-release/macos"
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            script = Path(temporary) / "deploy/macos/run-backend.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper = script.parent / "logging_wrapper.py"
            wrapper.write_text("# wrapper\n", encoding="utf-8")
            pid_path = pids / "backend.pid"
            instance_path = pids / "backend.instance"
            pid_path.write_text("321\n", encoding="utf-8")
            instance_path.write_text("legacy\n", encoding="utf-8")
            expected = {
                "pid": 321,
                "birth": "legacy-birth",
                "birth_precise": True,
                "pgid": 321,
                "sid": 321,
                "command": (
                    f"{runtime / 'venv/bin/python'} {wrapper.resolve()} -- "
                    f"{script.resolve()}"
                ),
            }
            with (
                mock.patch.object(module, "process_identity", side_effect=[expected, expected, None]),
                mock.patch.object(module.os, "kill") as kill,
                mock.patch.object(module, "_listener_pids", return_value=[]),
            ):
                module._stop_one(runtime, "backend", port=5001, script=script)
            kill.assert_called_once_with(321, signal.SIGTERM)
            self.assertFalse(pid_path.exists())
            self.assertFalse(instance_path.exists())

            pid_path.write_text("321\n", encoding="utf-8")
            mismatched = {**expected, "command": "python unrelated.py"}
            with (
                mock.patch.object(module, "process_identity", return_value=mismatched),
                mock.patch.object(module.os, "kill") as kill,
            ):
                with self.assertRaises(module.IdentityMismatch):
                    module._stop_one(runtime, "backend", port=5001, script=script)
            kill.assert_not_called()
            self.assertTrue(pid_path.exists())

    def test_stop_retains_metadata_when_listener_escapes_recorded_session(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            metadata_path = pids / "backend.meta.json"
            metadata = {
                "version": module.METADATA_VERSION,
                "service": "backend",
                "instance": "instance-1",
                "port": 55001,
                "endpoint": "http://127.0.0.1:55001/version",
                "service_sid": 401,
                "state": "running",
                "seq": 2,
                "updated_at": 1,
                "supervisor": {
                    "pid": 201,
                    "birth": "birth-201",
                    "pgid": 201,
                    "sid": 401,
                    "command": "python lifecycle.py",
                },
                "guard": {
                    "pid": 202,
                    "birth": "birth-202",
                    "pgid": 202,
                    "sid": 402,
                    "command": "guard",
                },
                "child": {
                    "pid": 203,
                    "birth": "birth-203",
                    "pgid": 203,
                    "sid": 401,
                    "command": "child",
                },
                "exit": None,
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with (
                mock.patch.object(module, "process_identity", return_value=None),
                mock.patch.object(module, "_session_members", return_value=[]),
                mock.patch.object(module, "_listener_pids", return_value=[999]),
            ):
                with self.assertRaises(module.LifecycleError):
                    module._stop_one(
                        runtime,
                        "backend",
                        port=55001,
                        script=MACOS_DEPLOY / "run-backend.sh",
                    )
            self.assertTrue(metadata_path.exists())

            with (
                mock.patch.object(module, "process_identity", return_value=None),
                mock.patch.object(module, "_session_members", return_value=[]),
                mock.patch.object(module, "_listener_pids", return_value=[999]),
                mock.patch("sys.stdout", io.StringIO()) as stdout,
            ):
                self.assertEqual(1, module._status(argparse.Namespace(runtime_dir=str(runtime))))
            payload = json.loads(stdout.getvalue())
            backend = next(item for item in payload if item["service"] == "backend")
            self.assertEqual("unknown", backend["state"])
            self.assertEqual("untracked", backend["listener"])

    def test_stop_escalates_exact_unresponsive_supervisor_and_guard(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            metadata_path = pids / "backend.meta.json"
            metadata = {
                "version": module.METADATA_VERSION,
                "service": "backend",
                "instance": "instance-1",
                "port": 55001,
                "endpoint": "http://127.0.0.1:55001/version",
                "service_sid": 401,
                "state": "running",
                "seq": 2,
                "updated_at": 1,
                "supervisor": {
                    "pid": 201,
                    "birth": "birth-201",
                    "pgid": 201,
                    "sid": 401,
                    "command": "python lifecycle.py",
                },
                "guard": {
                    "pid": 202,
                    "birth": "birth-202",
                    "pgid": 202,
                    "sid": 402,
                    "command": "guard",
                },
                "child": {
                    "pid": 203,
                    "birth": "birth-203",
                    "pgid": 203,
                    "sid": 401,
                    "command": "child",
                },
                "exit": None,
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            live = {201, 202, 203}

            def fake_identity(pid: int):
                if pid not in live:
                    return None
                role = {201: "supervisor", 202: "guard", 203: "child"}[pid]
                return {
                    **metadata[role],
                    "birth_precise": True,
                    "lstart": "lstart",
                    "state": "S",
                }

            def fake_kill(pid: int, signum: int) -> None:
                if (pid, signum) == (202, signal.SIGKILL):
                    live.discard(202)

            def fake_terminate(sid: int, *, exclude=(), grace=0.0) -> None:
                self.assertEqual(401, sid)
                self.assertEqual(set(), set(exclude))
                live.discard(201)
                live.discard(203)

            with (
                mock.patch.object(module, "process_identity", side_effect=fake_identity),
                mock.patch.object(module.os, "kill", side_effect=fake_kill) as kill,
                mock.patch.object(module, "_terminate_session", side_effect=fake_terminate) as terminate,
                mock.patch.object(module, "_session_members", return_value=[]),
                mock.patch.object(module, "_listener_pids", return_value=[]),
                mock.patch.object(module, "DEFAULT_GRACE_SECONDS", 0.01),
                mock.patch.object(module, "DEFAULT_READY_SECONDS", 1.0),
            ):
                module._stop_one(
                    runtime,
                    "backend",
                    port=55001,
                    script=MACOS_DEPLOY / "run-backend.sh",
                )
            kill.assert_any_call(201, signal.SIGTERM)
            kill.assert_any_call(202, signal.SIGKILL)
            terminate.assert_called_once()
            self.assertFalse(metadata_path.exists())

    def test_status_marks_valid_legacy_pid_as_migration_required(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            (pids / "backend.pid").write_text("321\n", encoding="utf-8")
            script = MACOS_DEPLOY / "run-backend.sh"
            actual = {
                "pid": 321,
                "birth": "legacy-birth",
                "birth_precise": True,
                "pgid": 321,
                "sid": 321,
                "command": (
                    f"{runtime / 'venv/bin/python'} "
                    f"{(MACOS_DEPLOY / 'logging_wrapper.py').resolve()} -- {script.resolve()}"
                ),
            }
            with (
                mock.patch.object(module, "process_identity", return_value=actual),
                mock.patch.object(module, "_listener_pids", return_value=[]),
                mock.patch("sys.stdout", io.StringIO()) as stdout,
            ):
                self.assertEqual(1, module._status(argparse.Namespace(runtime_dir=str(runtime))))
            payload = json.loads(stdout.getvalue())
            backend = next(item for item in payload if item["service"] == "backend")
            self.assertEqual("legacy-running", backend["state"])

    def test_session_enumeration_rejects_empty_and_malformed_ps_output(self) -> None:
        module = self._load_lifecycle()
        with mock.patch.object(
            module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="1 S\n", stderr=""),
        ):
            self.assertEqual([], module._session_members(123))
        for stdout in ("", "not-a-pid S\n", "123\n", "123 S extra\n"):
            with self.subTest(stdout=stdout), mock.patch.object(
                module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
            ):
                with self.assertRaises(module.IdentityUnknown):
                    module._session_members(123)

    def test_exact_signal_rejects_pid_reuse_immediately_before_kill(self) -> None:
        module = self._load_lifecycle()
        expected = {
            "pid": 201,
            "birth": "original",
            "birth_precise": True,
            "pgid": 201,
            "sid": 401,
            "command": "python lifecycle.py",
        }
        reused = {**expected, "birth": "reused"}
        with (
            mock.patch.object(module, "process_identity", return_value=reused),
            mock.patch.object(module.os, "kill") as kill,
        ):
            with self.assertRaises(module.IdentityMismatch):
                module._signal_exact(
                    expected,
                    signal.SIGTERM,
                    label="backend supervisor",
                    token="lifecycle.py",
                    service_sid=401,
                )
        kill.assert_not_called()

    def test_process_identity_treats_mid_inspection_exit_as_dead(self) -> None:
        module = self._load_lifecycle()
        with (
            mock.patch.object(module.os, "kill", side_effect=[None, ProcessLookupError()]),
            mock.patch.object(module, "_ps", side_effect=["S", None, None]),
        ):
            self.assertIsNone(module.process_identity(201))

        with (
            mock.patch.object(module.os, "kill", return_value=None),
            mock.patch.object(module, "_ps", side_effect=["S", "command", "lstart"]),
            mock.patch.object(module.os, "getpgid", side_effect=ProcessLookupError()),
        ):
            self.assertIsNone(module.process_identity(201))

    def test_out_of_range_metadata_and_legacy_pids_fail_closed(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            metadata_path = pids / "backend.meta.json"
            metadata = {
                "version": module.METADATA_VERSION,
                "service": "backend",
                "instance": "instance-1",
                "port": 5001,
                "endpoint": "http://127.0.0.1:5001/version",
                "service_sid": 401,
                "state": "running",
                "seq": 1,
                "updated_at": 1,
                "supervisor": {
                    "pid": module.MAX_PID + 1,
                    "birth": "birth",
                    "pgid": 401,
                    "sid": 401,
                    "command": "python lifecycle.py",
                },
                "guard": None,
                "child": None,
                "exit": None,
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaises(module.LifecycleError):
                module._read_service_metadata(metadata_path, "backend")
            self.assertIsNone(module.process_identity(module.MAX_PID + 1))

            metadata_path.unlink()
            legacy_path = pids / "backend.pid"
            legacy_path.write_text(f"{module.MAX_PID + 1}\n", encoding="utf-8")
            with self.assertRaises(module.LifecycleError):
                module._legacy_identity(
                    runtime,
                    "backend",
                    script=MACOS_DEPLOY / "run-backend.sh",
                )
            self.assertTrue(legacy_path.exists())

    def test_metadata_and_legacy_record_reads_refuse_links_and_oversize(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pids = root / "pids"
            pids.mkdir()
            source = root / "foreign.json"
            source.write_text("{}\n", encoding="utf-8")
            metadata_path = pids / "backend.meta.json"
            metadata_path.symlink_to(source)
            with self.assertRaises(module.LifecycleError):
                module._read_json(metadata_path)

            metadata_path.unlink()
            os.link(source, metadata_path)
            with self.assertRaises(module.LifecycleError):
                module._read_json(metadata_path)

            metadata_path.unlink()
            source.unlink()
            metadata_path.write_bytes(b"x" * (module.MAX_METADATA_BYTES + 1))
            with self.assertRaises(module.LifecycleError):
                module._read_json(metadata_path)

            metadata_path.unlink()
            legacy_target = root / "foreign.pid"
            legacy_target.write_text("321\n", encoding="utf-8")
            legacy_path = pids / "backend.pid"
            legacy_path.symlink_to(legacy_target)
            with mock.patch.object(module, "process_identity") as identity:
                with self.assertRaises(module.LifecycleError):
                    module._legacy_identity(
                        root,
                        "backend",
                        script=MACOS_DEPLOY / "run-backend.sh",
                    )
            identity.assert_not_called()

            legacy_path.unlink()
            fifo_path = pids / "backend.meta.json"
            os.mkfifo(fifo_path)
            probe = textwrap.dedent(
                f"""
                import importlib.util
                import sys
                from pathlib import Path

                spec = importlib.util.spec_from_file_location("fifo_lifecycle", {str(LIFECYCLE)!r})
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                try:
                    module._read_json(Path({str(fifo_path)!r}))
                except module.LifecycleError:
                    raise SystemExit(0)
                raise SystemExit(1)
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

    def test_record_cleanup_refuses_directory_without_partial_removal(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            pids = runtime / "pids"
            pids.mkdir()
            pid_path = pids / "backend.pid"
            instance_path = pids / "backend.instance"
            metadata_path = pids / "backend.meta.json"
            pid_path.write_text("321\n", encoding="utf-8")
            instance_path.mkdir()
            metadata_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(module.LifecycleError):
                module._remove_records(runtime, "backend")
            self.assertTrue(pid_path.exists())
            self.assertTrue(instance_path.is_dir())
            self.assertTrue(metadata_path.exists())

    def test_start_all_rejects_existing_listener_and_readiness_timeout(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            stderr = io.StringIO()
            with mock.patch.object(module, "_reconcile"), mock.patch.object(module, "_listener_pids", side_effect=lambda port: [999] if port == 5001 else []), mock.patch("sys.stderr", stderr):
                result = module.main(
                    [
                        "start-all",
                        "--runtime-dir",
                        str(runtime),
                        "--python-bin",
                        sys.executable,
                        "--backend-script",
                        str(runtime / "run-backend.sh"),
                        "--api-script",
                        str(runtime / "run-api.sh"),
                        "--backend-port",
                        "5001",
                        "--api-port",
                        "8000",
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn("port 5001 already has a listener", stderr.getvalue())

        process = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, backend_script = self._write_fixture(root)
            api_script = root / "run-api.sh"
            env = self._helper_env(
                runtime,
                registry,
                {
                    "DOCLING_BACKEND_PORT": str(_free_port()),
                    "DOCLING_API_PORT": str(_free_port()),
                    "DOCLING_MACOS_BACKEND_READY_ATTEMPTS": "2",
                    "FAKE_HEALTH_FAIL_SUBSTR": "/version",
                },
            )
            process = self._spawn_helper(
                helper,
                "start-all-patched",
                [
                    "--runtime-dir",
                    str(runtime),
                    "--python-bin",
                    sys.executable,
                    "--backend-script",
                    str(backend_script),
                    "--api-script",
                    str(api_script),
                    "--backend-port",
                    env["DOCLING_BACKEND_PORT"],
                    "--api-port",
                    env["DOCLING_API_PORT"],
                ],
                env,
            )
            try:
                stdout, stderr = self._read_output(process)
                self.assertEqual("", stdout)
                self.assertEqual(2, process.returncode)
                self.assertIn("backend did not become ready", stderr)
                if (runtime / "pids/backend.meta.json").exists():
                    metadata = _read_json(runtime / "pids/backend.meta.json")
                    self.assertFalse(_pid_alive(int(metadata["child"]["pid"])))
            finally:
                self._terminate_process(process)

    def test_supervisor_signal_statuses_return_128_plus_signal(self) -> None:
        for signum, expected in (
            (signal.SIGTERM, 143),
            (signal.SIGINT, 130),
            (signal.SIGHUP, 129),
        ):
            process = None
            ready_r = None
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime, registry, helper, backend_script = self._write_fixture(root)
                process, ready_r = self._spawn_supervisor(
                    helper,
                    runtime,
                    registry,
                    backend_script,
                    env_extra={"DOCLING_BACKEND_PORT": str(_free_port())},
                )
                metadata_path = runtime / "pids/backend.meta.json"
                try:
                    self.assertEqual(b"READY\n", os.read(ready_r, 64))
                    _wait_for(lambda: metadata_path.exists() and _read_json(metadata_path)["state"] == "running")
                    metadata = _read_json(metadata_path)
                    os.kill(process.pid, signum)
                    process.wait(timeout=8)
                    self.assertEqual(expected, process.returncode)
                    _wait_for(lambda: not _pid_alive(int(metadata["guard"]["pid"])))
                    _wait_for(lambda: not _pid_alive(int(metadata["child"]["pid"])))
                finally:
                    if ready_r is not None:
                        os.close(ready_r)
                    self._terminate_process(process)

    def test_status_reports_stale_and_unknown(self) -> None:
        module = self._load_lifecycle()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            pids = runtime / "pids"
            pids.mkdir(parents=True)
            base = {
                "version": module.METADATA_VERSION,
                "service": "backend",
                "instance": "instance-1",
                "port": 5001,
                "endpoint": "http://127.0.0.1:5001/version",
                "service_sid": 401,
                "state": "running",
                "seq": 2,
                "updated_at": 1,
                "supervisor": {"pid": 201, "birth": "birth-201", "pgid": 301, "sid": 401, "command": "python lifecycle.py"},
                "guard": {"pid": 202, "birth": "birth-202", "pgid": 302, "sid": 402, "command": "guard"},
                "child": {"pid": 203, "birth": "birth-203", "pgid": 303, "sid": 403, "command": "child"},
                "exit": None,
            }
            (pids / "backend.meta.json").write_text(json.dumps(base), encoding="utf-8")

            with mock.patch.object(module, "process_identity", return_value=None), mock.patch.object(module, "_session_members", return_value=[]), mock.patch.object(module, "_listener_pids", return_value=[]), mock.patch("sys.stdout", io.StringIO()) as stdout:
                self.assertEqual(1, module._status(argparse.Namespace(runtime_dir=str(runtime))))
                payload = json.loads(stdout.getvalue())
            backend = next(item for item in payload if item["service"] == "backend")
            self.assertEqual("stale", backend["state"])

            def fake_identity(pid: int):
                if pid == 201:
                    return {
                        "pid": 201,
                        "birth": "birth-201",
                        "birth_precise": True,
                        "lstart": "lstart",
                        "pgid": 301,
                        "sid": 401,
                        "command": "python wrong.py",
                        "state": "S",
                    }
                return None

            with mock.patch.object(module, "process_identity", side_effect=fake_identity), mock.patch.object(module, "_session_members", return_value=[]), mock.patch.object(module, "_listener_pids", return_value=[]), mock.patch("sys.stdout", io.StringIO()) as stdout:
                self.assertEqual(1, module._status(argparse.Namespace(runtime_dir=str(runtime))))
                payload = json.loads(stdout.getvalue())
            backend = next(item for item in payload if item["service"] == "backend")
            self.assertEqual("unknown", backend["state"])

    def test_legacy_rotation_is_still_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, registry, helper, _backend_script = self._write_fixture(root)
            process = self._spawn_helper(
                helper,
                "legacy-patched",
                [
                    "--log-path",
                    str(runtime / "logs/legacy.log"),
                    "--max-bytes",
                    "128",
                    "--backup-count",
                    "2",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 400)",
                ],
                self._helper_env(runtime, registry),
            )
            stdout, stderr = self._read_output(process)
            self.assertEqual(0, process.returncode, stderr)
            self.assertEqual("", stdout)
            self.assertTrue((runtime / "logs/legacy.log").exists())
            self.assertTrue((runtime / "logs/legacy.log.1").exists())
            self.assertTrue((runtime / "logs/legacy.log.2").exists())
            self.assertLessEqual((runtime / "logs/legacy.log").stat().st_size, 128)


if __name__ == "__main__":
    unittest.main()
