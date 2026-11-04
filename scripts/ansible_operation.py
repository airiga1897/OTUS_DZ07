"""Resume one DZ07 Ansible operation; transport failures are not Ansible failures."""

import contextlib
import hashlib
import json
import logging
import re
import shlex
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import paramiko
from inventory_types import Inventory
from progress_logging import WaitProgress, get_logger, timed
from remote.ansible_job import (
    MAX_OPERATION_TIMEOUT,
    MAX_STOP_WAIT,
    OPERATION_STATES,
    OperationBinding,
    OperationRequest,
    OperationState,
    checked_request,
)
from remote.ansible_job import (
    OperationReply as OperationReply,
)
from runtime_ports import ConfigurationTargetPort, FilePort, OperationPort, StandDescription
from stage_store import atomic_json

LOGGER = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "stopped"}


class OperationPolicy(TypedDict):
    operation_timeout_seconds: int
    connection_wait_seconds: int
    poll_interval_seconds: int
    stop_wait_seconds: int


class OperationContext(TypedDict):
    request: OperationRequest
    bootstrap: bool
    state: NotRequired[OperationState]
    acknowledged: NotRequired[bool]
    finalized: NotRequired[bool]
    finishing_pending: NotRequired[bool]
    stop_confirmed: NotRequired[bool]
    result: NotRequired[object]
    stop_result: NotRequired[object]
    progress_cursor: NotRequired[object]
    _progress_notice: NotRequired[object]
    _progress_warning: NotRequired[object]


def checked_reply(value: object, identity: str) -> OperationReply:
    if not isinstance(value, dict) or value.get("state") not in OPERATION_STATES:
        raise OperationProtocolError("Invalid operation response")
    if value.get("id", identity) != identity:
        raise OperationProtocolError("Operation identity differs")
    if "operation_result" in value:
        checked_reply(value["operation_result"], identity)
    return cast(OperationReply, value)


class OperationUnknown(RuntimeError):
    """Preserve the operation: connection or remote result is unconfirmed."""


class OperationFailed(RuntimeError):
    """Ansible/worker explicitly reported a nonzero completion."""


class OperationProtocolError(ValueError):
    pass


def policy(api: FilePort) -> OperationPolicy:
    result = json.loads((api.ROOT / "config/ansible-operation.json").read_text())
    if not isinstance(result, dict):
        raise ValueError("Invalid Ansible operation policy document")
    for name, maximum in [
        ("operation_timeout_seconds", MAX_OPERATION_TIMEOUT),
        ("connection_wait_seconds", 3600),
        ("poll_interval_seconds", 60),
        ("stop_wait_seconds", MAX_STOP_WAIT),
    ]:
        if type(result.get(name)) is not int or not 1 <= result[name] <= maximum:
            raise ValueError("Invalid Ansible operation policy: " + name)
    return cast(OperationPolicy, result)


def path(api: FilePort) -> Path:
    return api.LOCAL / "ansible-operation.json"


def read(api: FilePort) -> OperationContext | None:
    from local_runtime import checked_tree

    checked_tree(path(api), Path(api.ROOT).resolve())
    if not path(api).exists():
        return None
    try:
        value = json.loads(path(api).read_text())
        if not isinstance(value, dict):
            raise ValueError("Invalid operation document")
        checked_request(value.get("request"))
        if type(value.get("bootstrap")) is not bool:
            raise ValueError("Invalid bootstrap flag")
        for field in ("acknowledged", "finalized", "finishing_pending", "stop_confirmed"):
            if field in value and type(value[field]) is not bool:
                raise ValueError("Invalid operation flag")
        if "state" in value and value["state"] not in OPERATION_STATES:
            raise ValueError("Invalid operation state")
    except ValueError:
        raise OperationProtocolError("Invalid saved Ansible operation context; preserve recovery data") from None
    return cast(OperationContext, value)


def pending(api: FilePort) -> bool:
    value = read(api)
    return value is not None and not value.get("finalized", False)


def required(api: FilePort) -> OperationContext:
    value = read(api)
    if value is None:
        raise OperationProtocolError("Saved Ansible operation context is missing; preserve stand data")
    return value


def binding(api: FilePort, lab: StandDescription) -> OperationBinding:
    file = api.LOCAL / "staged-deployment.json"
    if file.exists():
        saved = json.loads(file.read_text())
        if saved["namespace"] != api.PREFIX:
            raise ValueError("Configuration namespace mismatch")
        deployment = saved["deployment_id"]
    else:
        # Retained legacy contexts also bind to a unique application VM.
        file = api.LOCAL / "context.json"
        saved = json.loads(file.read_text())
        deployment = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()[:32]
    return {"deployment_id": deployment, "namespace": api.PREFIX, "app_id": lab["server_ids"]["app"]}


def source_version(api: FilePort) -> str:
    import delivery

    if delivery.mode(api.ROOT) == "release":
        manifest = json.loads((api.LOCAL / "artifact-delivery.json").read_text())
        source = manifest.get("source") if isinstance(manifest, dict) else None
        digest = source.get("sha512") if isinstance(source, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{128}", digest):
            raise OperationProtocolError("Invalid saved release source digest; preserve recovery data")
        return digest
    from artifact_content import source_archive

    work = api.ROOT / ".tools/operation-build"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as directory:
        return source_archive(api.ROOT, Path(directory) / "source.tar.gz")


def _bootstrap(inventory: Inventory) -> bool:
    value = inventory["all"]["vars"].get("dz07_bootstrap")
    if type(value) is not bool:
        raise OperationProtocolError("Invalid Ansible bootstrap flag; operation was not created")
    return value


def payload(api: FilePort, inventory: Inventory) -> dict[str, str]:
    return {
        "key": (api.LOCAL / "otus_dz07").read_text(),
        "known_hosts": (api.LOCAL / "known_hosts").read_text(),
        "inventory.json": json.dumps(inventory, sort_keys=True),
        "controls.json": json.dumps({"dz07_bootstrap": _bootstrap(inventory)}, sort_keys=True),
    }


def request(api: FilePort, lab: StandDescription, inventory: Inventory, identity: str) -> OperationRequest:
    options = policy(api)
    hashes = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in payload(api, inventory).items()}
    parameters = {"payload": hashes, "source": source_version(api), "policy": options}
    return checked_request(
        {
            "id": identity,
            **binding(api, lab),
            "payload": hashes,
            "parameters_sha256": hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest(),
            "timeout": options["operation_timeout_seconds"],
            "stop_wait": options["stop_wait_seconds"],
        }
    )


def bounded_ssh[Value](client: paramiko.SSHClient, action: Callable[[], Value], timeout: float) -> Value:
    """Paramiko's exec/subsystem acknowledgements can wait without a timeout."""
    done = threading.Event()
    values: list[Value] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            values.append(action())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    threading.Thread(target=invoke, daemon=True).start()
    if not done.wait(timeout):
        try:
            client.close()
        except Exception:
            LOGGER.warning("SSH close failed after transport deadline")
        raise TimeoutError("Operation SSH transport deadline exceeded")
    if errors:
        raise errors[0]
    return values[0]


def command(client: paramiko.SSHClient, text: str, data: object = None, timeout: int = 15) -> str:
    return bounded_ssh(client, lambda: command_body(client, text, data, timeout), timeout)


def command_body(client: paramiko.SSHClient, text: str, data: object, timeout: int) -> str:
    """Short RPC deadline, independent of the systemd job's deadline."""
    transport = client.get_transport()
    if transport is None:
        raise OSError("Operation SSH transport unavailable")
    channel = transport.open_session(timeout=timeout)
    output, errors = bytearray(), bytearray()
    try:
        channel.settimeout(timeout)
        channel.exec_command(text)
        if data is not None:
            channel.sendall(json.dumps(data).encode())
        channel.shutdown_write()
        deadline = time.monotonic() + timeout
        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if time.monotonic() >= deadline or not transport.is_active():
                raise TimeoutError("Operation RPC connection unavailable")
            if channel.recv_ready():
                output.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(65536))
            time.sleep(0.05)
        if channel.recv_exit_status():
            raise OperationProtocolError("Remote operation protocol rejected the request; private state preserved")
        return output.decode()
    finally:
        channel.close()


def install(api: OperationPort, client: paramiko.SSHClient) -> None:
    remote = api.STACK["paths"]["remote_root"]
    command(client, "install -d -m 700 " + shlex.quote(remote))
    put_private(client, remote + "/.ansible-operation.py", (api.ROOT / "scripts/remote/ansible_job.py").read_bytes())
    try:
        command(client, "install -d -m 700 " + shlex.quote(remote + "/remote"))
        for source, target in (
            ("ansible_events.py", "/remote/ansible_events.py"),
            ("ansible_callback.py", "/.ansible-callback.py"),
        ):
            put_private(client, remote + target, (api.ROOT / "scripts/remote" / source).read_bytes())
    except Exception:
        with contextlib.suppress(Exception):
            get_logger(__name__).warning(
                "Ansible: доставка наблюдателя прогресса не подтверждена; основной протокол операции сохранён"
            )


def put_private(client: paramiko.SSHClient, path: str, content: str | bytes) -> None:
    def transfer() -> None:
        with client.open_sftp() as sftp:
            channel = sftp.get_channel()
            if channel is None:
                raise OSError("Operation SFTP channel unavailable")
            channel.settimeout(15)
            with sftp.open(path, "wb") as stream:
                stream.write(content.encode() if isinstance(content, str) else content)
            sftp.chmod(path, 0o600)

    bounded_ssh(client, transfer, 15)


def rpc(api: OperationPort, lab: StandDescription, action: str, operation: OperationRequest) -> OperationReply:
    deadline = time.monotonic() + policy(api)["connection_wait_seconds"]
    current = action
    while True:
        try:
            with api.connect(lab) as client:
                if current in ("reserve", "stop"):
                    install(api, client)
                remote = api.STACK["paths"]["remote_root"]
                raw = command(
                    client,
                    "python3 "
                    + shlex.quote(remote + "/.ansible-operation.py")
                    + " --root "
                    + shlex.quote(remote)
                    + " rpc",
                    {"action": current, "request": operation},
                )
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    raise OperationUnknown("Operation response was lost or incomplete") from None
                return checked_reply(result, operation["id"])
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
            raise OperationProtocolError("SSH authentication or host identity is unconfirmed") from None
        except (OSError, paramiko.SSHException, OperationUnknown):
            # Start may have succeeded. Ask about that same unit before retrying
            # any idempotent start request; never allocate a replacement ID.
            if current == "start":
                current = "status"
            if time.monotonic() >= deadline:
                raise OperationUnknown(
                    "Connection unavailable; the same Ansible operation may still be running"
                ) from None
            time.sleep(policy(api)["poll_interval_seconds"])


def save(api: FilePort, value: OperationContext) -> None:
    atomic_json(path(api), value, api.ROOT)


def record(api: FilePort, value: OperationContext, result: OperationReply) -> None:
    value["state"] = result["state"]
    value["result"] = result
    save(api, value)
    safe: dict[str, object] = {"state": result["state"]}
    if result.get("phase") in ("prepare", "syntax", "configure"):
        safe["phase"] = result["phase"]
    if type(result.get("exit_code")) is int:
        safe["exit_code"] = result["exit_code"]
    reasons = {
        "operation_timeout",
        "stopped",
        "worker_error",
        "service_ended_without_result",
        "incomplete_result",
        "result_missing_or_invalid",
    }
    reason = result.get("reason")
    if isinstance(reason, str) and reason in reasons:
        safe["reason"] = reason
    metadata: Mapping[str, object] = result
    for key in ("created_utc", "started_utc", "finished_utc"):
        try:
            text = metadata.get(key)
            if not isinstance(text, str):
                continue
            stamp = datetime.fromisoformat(text)
            if stamp.tzinfo is not None:
                safe[key] = stamp.isoformat()
        except (KeyError, TypeError, ValueError):
            pass
    safe["operation_id"] = value["request"]["id"]
    try:
        atomic_json(api.ROOT / "docs/ansible-operation-result.json", safe, api.ROOT)
    except OSError as error:
        LOGGER.warning("Cannot save public operation evidence (%s)", type(error).__name__)


def validate(api: FilePort, lab: StandDescription, inventory: Inventory, value: OperationContext) -> None:
    expected = request(api, lab, inventory, value["request"]["id"])
    if expected != value["request"]:
        raise OperationProtocolError(
            "Stand, application VM or Ansible parameters changed; preserve the pending operation"
        )


def reserve(api: OperationPort, lab: StandDescription, inventory: Inventory) -> OperationReply:
    value = read(api)
    if value is None or value.get("finalized"):
        value = OperationContext(
            request=request(api, lab, inventory, uuid.uuid4().hex),
            bootstrap=_bootstrap(inventory),
            state="preparing",
            acknowledged=False,
            finalized=False,
        )
    else:
        validate(api, lab, inventory, value)
    save(api, value)  # Save before any SSH mutation or possible lost reply.
    result = rpc(api, lab, "reserve", value["request"])
    record(api, value, result)
    return result


@timed("Ansible: наблюдение за сохранённой операцией на app")
def wait(api: OperationPort, lab: StandDescription, inventory: Inventory, *, upload: bool = False) -> OperationReply:
    value = required(api)
    validate(api, lab, inventory, value)
    from ansible_progress import observe

    progress = WaitProgress(get_logger(__name__), "Ansible на app")
    try:
        result = rpc(api, lab, "status", value["request"])
        if upload and result["state"] == "preparing":
            with api.connect(lab) as client:
                remote = api.STACK["paths"]["remote_root"] + "/.ansible-operations/" + value["request"]["id"]
                for name, content in payload(api, inventory).items():
                    put_private(client, remote + "/" + name, content)
        while result["state"] in ("preparing", "running"):
            observe(api, lab, value, command, save, operation_state=result["state"])
            progress.status(result["state"])
            record(api, value, result)
            if result["state"] == "preparing":
                result = rpc(api, lab, "start", value["request"])
            else:
                time.sleep(policy(api)["poll_interval_seconds"])
                result = rpc(api, lab, "status", value["request"])
        record(api, value, result)
        observe(api, lab, value, command, save, operation_state=result["state"])
        progress.status(result["state"])
        if result["state"] == "unknown":
            raise OperationUnknown("Operation result is unconfirmed; preserve private state and retry observation")
        if result["state"] != "succeeded":
            raise OperationFailed("Ansible operation " + result["state"] + "; exit=" + str(result.get("exit_code")))
        return result
    except (OperationUnknown, OSError, paramiko.SSHException) as error:
        value["state"] = "unknown"
        save(api, value)
        if isinstance(error, OperationUnknown):
            raise
        raise OperationUnknown("Connection lost; operation ID and recovery data preserved") from None


def finish(api: OperationPort, lab: StandDescription, primary: Exception | None = None) -> None:
    value = required(api)
    try:
        import observability

        deadline = time.monotonic() + policy(api)["connection_wait_seconds"]
        while True:
            try:
                with api.connect(lab) as app:
                    observability.package_cache(
                        api, app, lab, start=False, remote=lambda client, text, label: command(client, text)
                    )
                break
            except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
                raise OperationProtocolError("SSH identity is unconfirmed during final actions") from None
            except (OSError, paramiko.SSHException):
                if time.monotonic() >= deadline:
                    raise OperationUnknown("Final actions connection unavailable") from None
                time.sleep(policy(api)["poll_interval_seconds"])
        rpc(api, lab, "acknowledge", value["request"])
        value["acknowledged"] = True
        value.pop("finishing_pending", None)
        if primary is not None:
            value["finalized"] = True
        try:
            save(api, value)
        except OSError:
            if primary is None:
                raise
            LOGGER.warning("Cannot save final-action state; primary error preserved")
    except Exception as error:
        value["finishing_pending"] = True
        try:
            save(api, value)
        except OSError:
            if primary is None:
                raise
            LOGGER.warning("Cannot save final-action state; primary error preserved")
        if primary is None:
            raise OperationUnknown(
                "Ansible finished; final actions remain unconfirmed, resume the saved operation"
            ) from None
        LOGGER.warning("Final operation actions failed (%s); primary error preserved", type(error).__name__)


def configure(api: OperationPort, app: paramiko.SSHClient | None, inventory: Inventory) -> None:
    lab = api.lab_output()
    if not pending(api):
        reserve(api, lab, inventory)
    try:
        wait(api, lab, inventory, upload=True)
    except OperationFailed as error:
        finish(api, lab, primary=error)
        raise
    finish(api, lab)


def resume(api: OperationPort, inventory: Inventory) -> bool:
    if not pending(api):
        return False
    lab = api.lab_output()
    value = required(api)
    validate(api, lab, inventory, value)
    result = rpc(api, lab, "reserve", value["request"])
    if result["state"] == "preparing":
        return False
    try:
        wait(api, lab, inventory)
    except OperationFailed as error:
        finish(api, lab, primary=error)
        raise
    finish(api, lab)
    return True


def finalize(api: FilePort) -> None:
    value = read(api)
    if value is not None:
        if not value.get("acknowledged"):
            raise OperationUnknown("Configuration operation is not acknowledged")
        value["finalized"] = True
        save(api, value)


def before_destroy(api: OperationPort, provider: ConfigurationTargetPort | None = None) -> None:
    value = read(api)
    if value is None:
        return
    if provider is not None:
        target = provider.configuration_target()
        lab, deleted = target.lab, target.deletion_verified
    else:
        lab, deleted = api.lab_output(), False
    if binding(api, lab) != {k: value["request"][k] for k in ("deployment_id", "namespace", "app_id")}:
        raise OperationProtocolError("Destroy operation belongs to another stand or VM")
    if value.get("stop_confirmed"):
        return
    if deleted:
        # This stage is marked deleted only after its state/API deletion audit.
        value["stop_confirmed"] = True
        save(api, value)
        return
    rpc(api, lab, "reserve", value["request"])
    result = rpc(api, lab, "stop", value["request"])
    if result["state"] != "stopped":
        raise OperationUnknown("Ansible process stop unconfirmed; VM deletion blocked")
    if result.get("operation_result", {}).get("state") in TERMINAL:
        record(api, value, result["operation_result"])
    value["stop_result"] = result
    value["stop_confirmed"] = True
    save(api, value)
