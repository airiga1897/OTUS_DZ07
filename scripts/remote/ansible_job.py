"""DZ07-only systemd Ansible operation protocol. No credentials in RPC output."""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal, NotRequired, TypedDict, cast

OperationState = Literal["preparing", "running", "unknown", "succeeded", "failed", "stopped"]
OPERATION_STATES: tuple[OperationState, ...] = ("preparing", "running", "unknown", "succeeded", "failed", "stopped")
MAX_OPERATION_TIMEOUT = 86400
MAX_STOP_WAIT = 120


class OperationBinding(TypedDict):
    deployment_id: str
    namespace: str
    app_id: str


class OperationRequest(OperationBinding):
    id: str
    parameters_sha256: str
    payload: dict[str, str]
    timeout: int
    stop_wait: int


class OperationReply(TypedDict):
    state: OperationState
    id: NotRequired[str]
    parameters_sha256: NotRequired[str]
    phase: NotRequired[object]
    reason: NotRequired[object]
    exit_code: NotRequired[object]
    created_utc: NotRequired[object]
    started_utc: NotRequired[object]
    finished_utc: NotRequired[object]
    operation_result: NotRequired["OperationReply"]


class SavedRequest(TypedDict):
    request: OperationRequest
    boot_id: str


class UnitState(TypedDict):
    active: bool
    loaded: bool


class UnitIdentity(TypedDict):
    namespace: str
    id: str


def checked_request(value: object) -> OperationRequest:
    if not isinstance(value, dict):
        raise ValueError("Invalid operation binding")
    for field, pattern in (
        ("id", r"[a-f0-9]{32}"),
        ("deployment_id", r"[a-f0-9]{32}"),
        ("namespace", r"[a-z][a-z0-9-]+"),
        ("app_id", r"[A-Za-z0-9-]+"),
        ("parameters_sha256", r"[a-f0-9]{64}"),
    ):
        if not isinstance(value.get(field), str) or not re.fullmatch(pattern, value[field]):
            raise ValueError("Invalid operation binding")
    for field, maximum in (("timeout", MAX_OPERATION_TIMEOUT), ("stop_wait", MAX_STOP_WAIT)):
        if type(value.get(field)) is not int or not 1 <= value[field] <= maximum:
            raise ValueError("Invalid operation binding")
    payload = value.get("payload")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"inventory.json", "controls.json", "key", "known_hosts"}
        or any(not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha) for sha in payload.values())
    ):
        raise ValueError("Invalid private payload binding")
    return cast(OperationRequest, value)


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(".next")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(path: Path, nonblocking: bool = False) -> Iterator[None]:
    if sys.platform == "win32":
        raise OSError("Ansible operation locking requires the Linux application VM")
    import fcntl

    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def signal_group(pid: int, force: bool = False) -> None:
    if sys.platform == "win32":
        raise OSError("Ansible process groups require the Linux application VM")
    os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)


class Manager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.home = self.root / ".ansible-operations"
        if self.home.is_symlink():
            raise ValueError("Operation directory link forbidden")
        self.home.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.home, 0o700)

    def folder(self, identity: str) -> Path:
        if not isinstance(identity, str) or not re.fullmatch("[a-f0-9]{32}", identity):
            raise ValueError("Invalid operation identity")
        path = self.home / identity
        if path.is_symlink():
            raise ValueError("Operation link forbidden")
        return path

    def unit_name(self, request: UnitIdentity) -> str:
        return request["namespace"] + "-ansible-" + request["id"] + ".service"

    def validate(self, request: object) -> OperationRequest:
        value = checked_request(request)
        self.folder(value["id"])
        return value

    def boot(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def saved(self, request: OperationRequest) -> SavedRequest:
        saved = json.loads((self.folder(request["id"]) / "request.json").read_text())
        if not isinstance(saved, dict) or saved.get("request") != request or not isinstance(saved.get("boot_id"), str):
            raise ValueError("Operation parameters or ownership differ")
        return cast(SavedRequest, saved)

    def unit(self, request: UnitIdentity) -> UnitState:
        value = subprocess.run(
            [
                "systemctl",
                "show",
                self.unit_name(request),
                "--no-pager",
                "--property=LoadState,ActiveState,ControlGroup,ExecMainStatus,Result",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        properties = dict(line.split("=", 1) for line in value.stdout.splitlines() if "=" in line)
        if value.returncode and properties.get("LoadState") != "not-found":
            raise RuntimeError("Cannot inspect operation service")
        if properties.get("LoadState") not in ("loaded", "not-found"):
            raise RuntimeError("Unknown operation service state")
        active = properties.get("ActiveState") not in ("inactive", "failed")
        group = properties.get("ControlGroup")
        if group:
            base = Path("/sys/fs/cgroup").resolve()
            path = (base / group.lstrip("/")).resolve()
            if not path.is_relative_to(base):
                raise ValueError("Invalid service control group")
            active |= any(file.read_text().strip() for file in path.rglob("cgroup.procs"))
        return {"active": active, "loaded": properties["LoadState"] == "loaded"}

    def status(self, request: OperationRequest) -> OperationReply:
        saved = self.saved(request)
        folder = self.folder(request["id"])
        unit = self.unit(request)
        try:
            raw = json.loads((folder / "result.json").read_text())
            if not isinstance(raw, dict):
                raise ValueError("Invalid result document")
            result = cast(OperationReply, raw)
            if result.get("id") != request["id"] or result.get("parameters_sha256") != request["parameters_sha256"]:
                raise ValueError("Invalid result binding")
            if result.get("state") not in ("preparing", "running", "succeeded", "failed", "stopped"):
                raise ValueError("Invalid result state")
        except (OSError, ValueError):
            return {"state": "running" if unit["active"] else "unknown", "reason": "result_missing_or_invalid"}
        if unit["active"]:
            return {**result, "state": "running"}
        if result["state"] == "running" or (
            result["state"] == "preparing" and (saved["boot_id"] != self.boot() or unit["loaded"])
        ):
            return {**result, "state": "unknown", "reason": "service_ended_without_result"}
        if result["state"] in ("succeeded", "failed") and (
            type(result.get("exit_code")) is not int
            or not result.get("started_utc")
            or not result.get("finished_utc")
            or (result["state"] == "succeeded" and result["exit_code"] != 0)
        ):
            return {**result, "state": "unknown", "reason": "incomplete_result"}
        return result

    def reserve(self, value: object) -> OperationReply:
        request = self.validate(value)
        with locked(self.home / "lock"):
            binding = {k: request[k] for k in ("deployment_id", "app_id", "namespace")}
            path = self.home / "binding.json"
            if path.exists() and json.loads(path.read_text()) != binding:
                raise ValueError("Application VM belongs to another deployment")
            active = self.home / "active.json"
            if active.exists() and json.loads(active.read_text())["id"] != request["id"]:
                raise ValueError("Another configuration operation must be acknowledged or stopped")
            folder = self.folder(request["id"])
            if folder.exists():
                self.saved(request)
            else:
                folder.mkdir(mode=0o700)
                atomic(folder / "request.json", {"request": request, "boot_id": self.boot()})
                atomic(
                    folder / "result.json",
                    {
                        "id": request["id"],
                        "parameters_sha256": request["parameters_sha256"],
                        "state": "preparing",
                        "created_utc": now(),
                        "started_utc": None,
                        "finished_utc": None,
                        "exit_code": None,
                    },
                )
            atomic(path, binding)
            atomic(active, {"id": request["id"]})
            return self.status(request)

    def start(self, request: OperationRequest) -> OperationReply:
        with locked(self.home / "lock"):
            self.saved(request)
            result = self.status(request)
            if result["state"] != "preparing":
                return result
            folder = self.folder(request["id"])
            if not (folder / "source").exists():
                with tempfile.TemporaryDirectory(dir=folder) as temp:
                    snapshot = Path(temp) / "source"
                    snapshot.mkdir()
                    for name in ("ansible", "scripts", "config"):
                        shutil.copytree(self.root / name, snapshot / name, ignore=shutil.ignore_patterns("__pycache__"))
                    snapshot.rename(folder / "source")
            for name, sha in request["payload"].items():
                if name not in ("inventory.json", "controls.json", "key", "known_hosts"):
                    raise ValueError("Unexpected private operation payload")
                with (folder / name).open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                        raise ValueError("Incomplete operation payload")
                os.chmod(folder / name, 0o600)
            shutil.copyfile(__file__, folder / "runner.py")
            os.chmod(folder / "runner.py", 0o600)
            try:
                plugins = folder / "callback_plugins"
                if not plugins.exists():
                    plugins.mkdir(mode=0o700)
                    (folder / "remote").mkdir(mode=0o700, exist_ok=True)
                    shutil.copyfile(self.root / "remote/ansible_events.py", folder / "remote/ansible_events.py")
                    shutil.copyfile(self.root / ".ansible-callback.py", plugins / "dz07_progress.py")
                    for file in (folder / "remote/ansible_events.py", plugins / "dz07_progress.py"):
                        os.chmod(file, 0o600)
            except OSError:
                pass  # A missing observer never changes whether Ansible can start.
            # The unit name is stable: a lost RPC response cannot create a second job.
            if not self.unit(request)["loaded"]:
                subprocess.run(
                    [
                        "sudo",
                        "-n",
                        "systemd-run",
                        "--quiet",
                        "--unit=" + self.unit_name(request),
                        "--uid=otus",
                        "--property=UMask=0077",
                        "--property=KillMode=control-group",
                        "--property=TimeoutStopSec=" + str(request["stop_wait"]),
                        "--property=SendSIGKILL=yes",
                        "--property=RuntimeMaxSec=" + str(request["timeout"] + request["stop_wait"] + 10),
                        "--property=Restart=no",
                        "/usr/bin/python3",
                        str(folder / "runner.py"),
                        "--root",
                        str(self.root),
                        "run",
                        request["id"],
                    ],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
            return self.status(request)

    def stop(self, request: OperationRequest) -> OperationReply:
        with locked(self.home / "lock"):
            self.saved(request)
            if self.unit(request)["loaded"]:
                subprocess.run(
                    ["sudo", "-n", "systemctl", "stop", self.unit_name(request)],
                    check=True,
                    capture_output=True,
                    timeout=request["stop_wait"] + 15,
                )
            if self.unit(request)["active"]:
                raise RuntimeError("Operation or child processes still active")
            previous = self.status(request)
            result: OperationReply = {
                "id": request["id"],
                "parameters_sha256": request["parameters_sha256"],
                "state": "stopped",
                "finished_utc": now(),
                "exit_code": 143,
            }
            if previous["state"] in ("succeeded", "failed", "stopped"):
                # Stop confirmation is separate from the Ansible outcome.
                # A finished playbook's code and timestamps must not be lost.
                return {**result, "operation_result": previous}
            atomic(self.folder(request["id"]) / "result.json", result)
            return result

    def events(self, request: OperationRequest, position: object = None) -> dict[str, object]:
        self.saved(request)  # Full stand/VM/operation binding before reading bytes.
        response = {"binding": request, "available": False}
        try:
            from remote.ansible_events import read_failure, read_page

            folder = self.folder(request["id"])
            if (folder / "events.unavailable").exists():
                return {**response, "reason": read_failure(folder / "events.unavailable")}
            if not (folder / "events.jsonl").exists():
                result = json.loads((folder / "result.json").read_text())
                if result["state"] in ("preparing", "running"):
                    return {**response, "pending": True}
                if result.get("phase") != "configure":
                    return {**response, "reason": "not_started"}
                return {**response, "reason": "journal_missing"}
            page = read_page(folder / "events.jsonl", position)
            if not page["events"] and (folder / "events.jsonl").stat().st_size > page["cursor"]["offset"]:
                result = json.loads((folder / "result.json").read_text())
                if result["state"] in ("succeeded", "failed", "stopped"):
                    return {**response, "reason": "journal_incomplete"}
            return {**response, "available": True, **page}
        except PermissionError:
            return {**response, "reason": "journal_permission_denied"}
        except OSError:
            return {**response, "reason": "journal_read_unavailable"}
        except (ValueError, TypeError, KeyError):
            return {**response, "reason": "journal_invalid"}
        except ImportError:
            return {**response, "reason": "observer_module_missing"}
        except Exception:
            return {**response, "reason": "observer_internal_error"}

    def acknowledge(self, request: OperationRequest) -> OperationReply:
        with locked(self.home / "lock"):
            result = self.status(request)
            if result["state"] not in ("succeeded", "failed", "stopped") or self.unit(request)["active"]:
                raise ValueError("Operation completion is unconfirmed")
            path = self.home / "active.json"
            if path.exists():
                if json.loads(path.read_text())["id"] != request["id"]:
                    raise ValueError("Another operation owns configuration")
                path.unlink()
            return result


def execute(manager: Manager, identity: str) -> None:
    folder = manager.folder(identity)
    request = checked_request(json.loads((folder / "request.json").read_text())["request"])
    with locked(folder / "runner.lock", nonblocking=True):
        result = manager.status(request)
        # systemd may be activating; only a previously persisted start prevents re-entry.
        if result.get("started_utc") or result.get("state") == "stopped":
            return
        result.update(
            {
                "id": identity,
                "parameters_sha256": request["parameters_sha256"],
                "state": "running",
                "started_utc": now(),
            }
        )
        atomic(folder / "result.json", result)
        child = None

        def stopped(signum: int, frame: FrameType | None) -> None:
            raise InterruptedError("Operation stopped")

        old_handler = signal.signal(signal.SIGTERM, stopped)
        deadline = time.monotonic() + request["timeout"]
        phase, code, reason = "prepare", 1, None
        try:
            inventory = json.loads((folder / "inventory.json").read_text())
            inventory["all"]["vars"].update(
                ansible_ssh_private_key_file=str(folder / "key"),
                ansible_ssh_common_args="-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o UserKnownHostsFile="
                + str(folder / "known_hosts"),
            )
            atomic(folder / "inventory.json", inventory)
            source = folder / "source"
            stack = json.loads((source / "config/stack.json").read_text())
            command = [
                stack["ansible"]["venv"] + "/bin/ansible-playbook",
                "--extra-vars",
                "@" + str(folder / "controls.json"),
                "-i",
                str(folder / "inventory.json"),
                "site.yml",
            ]
            steps = [
                ("prepare", ["sudo", "-n", "/usr/bin/python3", str(source / "scripts/remote/ansible_control.py")]),
                ("syntax", command + ["--syntax-check"]),
                ("configure", command),
            ]
            environment = dict(os.environ, ANSIBLE_CONFIG=str(source / "ansible/ansible.cfg"))
            with (folder / "operation.log").open("a") as log:
                os.chmod(folder / "operation.log", 0o600)
                for phase, args in steps:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, request["timeout"])
                    observed = dict(environment)
                    if phase == "configure" and (folder / "callback_plugins/dz07_progress.py").is_file():
                        observed.update(
                            ANSIBLE_CALLBACK_PLUGINS=str(folder / "callback_plugins"),
                            ANSIBLE_CALLBACKS_ENABLED="dz07_progress",
                            DZ07_PROGRESS_FILE=str(folder / "events.jsonl"),
                        )
                    child = subprocess.Popen(
                        args, cwd=source / "ansible", env=observed, stdout=log, stderr=log, start_new_session=True
                    )
                    code = child.wait(timeout=remaining)
                    if code:
                        break
        except subprocess.TimeoutExpired:
            code, reason = 124, "operation_timeout"
        except InterruptedError:
            code, reason = 143, "stopped"
        except Exception:
            code, reason = 1, "worker_error"
        finally:
            if child is not None and child.poll() is None:
                try:
                    signal_group(child.pid)
                    child.wait(timeout=request["stop_wait"])
                except subprocess.TimeoutExpired:
                    signal_group(child.pid, force=True)
                    child.wait()
                except (ProcessLookupError, PermissionError):
                    # systemd owns the entire control group, including sudo
                    # descendants. It kills residual children on service exit;
                    # status() stays running until the group is empty.
                    pass
            signal.signal(signal.SIGTERM, old_handler)
            result.update(
                {
                    "state": "succeeded" if code == 0 else "stopped" if code == 143 else "failed",
                    "phase": phase,
                    "exit_code": code,
                    "reason": reason,
                    "finished_utc": now(),
                }
            )
            atomic(folder / "result.json", result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("action", choices=["rpc", "run"])
    parser.add_argument("id", nargs="?")
    args = parser.parse_args()
    manager = Manager(args.root)
    if args.action == "run":
        execute(manager, args.id)
    else:
        data = json.load(sys.stdin)
        action, request = data["action"], data["request"]
        manager.validate(request)
        if action not in ("reserve", "start", "status", "stop", "acknowledge", "events"):
            raise ValueError("Unknown operation action")
        result = (
            manager.events(request, data.get("cursor")) if action == "events" else getattr(manager, action)(request)
        )
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Ansible operation protocol failed: " + type(error).__name__) from None
