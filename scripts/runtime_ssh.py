"""Explicit runtime operations: ssh."""

import base64
import io
import json
import shlex
import tarfile
import time

import paramiko
import topology
from cloud_settings import terraform_root
from progress_logging import get_logger
from runtime_ports import StandDescription, TransportPort

LOGGER = get_logger(__name__)


def _internal_transport(client: paramiko.SSHClient) -> paramiko.Transport:
    transport = client.get_transport()
    if transport is None:
        raise OSError("SSH transport unavailable")
    return transport


def trust(api: TransportPort, lab: StandDescription) -> None:
    from stage_store import deployment_exists

    staged = deployment_exists(api.ROOT)
    if staged:
        from staged_runtime import backend

        provider = backend(api)
    else:
        cloud = api.cloud()
    cache_path = api.LOCAL / "trusted-hosts.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    lines = []
    for name, server_id in lab["server_ids"].items():
        if staged:
            key = provider.ssh_host_key(lab, name, cache.get(server_id))
        else:
            node_cloud = cloud.read_only_pool(lab["node_regions"][name]) if lab.get("node_regions") else cloud
            key = node_cloud.ssh_host_key(name, server_id, api.PREFIX, cache.get(server_id))
        paramiko.Ed25519Key(data=base64.b64decode(key))
        cache[server_id] = key
        api.save(cache_path, cache)
        hosts = [lab["private_ips"][name]] + ([lab["app_public_ip"]] if name == "app" else [])
        lines.append(",".join(hosts) + " ssh-ed25519 " + key + "\n")
    (api.LOCAL / "known_hosts").write_text("".join(lines), encoding="utf-8")


def connect(
    api: TransportPort, lab: StandDescription, name: str = "app", front: paramiko.SSHClient | None = None
) -> paramiko.SSHClient:
    host = lab["app_public_ip"] if name == "app" else lab["private_ips"][name]
    client = paramiko.SSHClient()
    sock = None
    try:
        client.load_host_keys(str(api.LOCAL / "known_hosts"))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        sock = _internal_transport(front).open_channel("direct-tcpip", (host, 22), ("127.0.0.1", 0)) if front else None
        client.connect(
            host,
            port=22,
            username="otus",
            key_filename=str(api.LOCAL / "otus_dz07"),
            sock=sock,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            banner_timeout=30,
        )
        if name == "app" and front is None:
            from operator_network import verify_ssh_source

            _, output, _ = client.exec_command('printf "%s" "$SSH_CONNECTION"', timeout=20)
            from stage_store import deployment_exists as exists

            variable_path = (
                api.LOCAL / "staged-deployment.json"
                if exists(api.ROOT)
                else terraform_root(api.ROOT) / "terraform.tfvars.json"
            )
            variables = json.loads(variable_path.read_text())
            verify_ssh_source(output.read().decode("ascii").strip(), variables["admin_cidrs"])
        return client
    except Exception:
        client.close()
        if sock is not None:
            sock.close()
        raise


def remote(
    api: TransportPort,
    client: paramiko.SSHClient,
    command: str,
    label: str = "remote",
    stdin: str | bytes | None = None,
) -> str:
    print(label + ": started", flush=True)
    channel = _internal_transport(client).open_session(timeout=30)
    # stdout is a machine-readable contract for JSON callers. Django and other
    # tools write diagnostics to stderr even when the command succeeds.
    channel.set_combine_stderr(False)
    out, err = bytearray(), bytearray()
    try:
        channel.exec_command(command)
        if stdin is not None:
            channel.sendall(stdin.encode() if isinstance(stdin, str) else stdin)
            channel.shutdown_write()
        deadline = time.monotonic() + 1800
        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("Remote command timeout: " + label)
            received = False
            if channel.recv_ready():
                out.extend(channel.recv(65536))
                received = True
            if channel.recv_stderr_ready():
                err.extend(channel.recv_stderr(65536))
                received = True
            if not received:
                time.sleep(0.1)
        text, diagnostic = out.decode(errors="replace"), err.decode(errors="replace")
        api.RUN.mkdir(parents=True, exist_ok=True)
        with (api.RUN / (label + ".log")).open("a", encoding="utf-8") as log:
            log.write("[stdout]\n" + api.redact(text) + "\n[stderr]\n" + api.redact(diagnostic))
        if channel.recv_exit_status():
            print(api.redact((text + "\n" + diagnostic)[-3000:]), flush=True)
            raise RuntimeError("Remote command failed: " + label)
        print(label + ": OK", flush=True)
        return text
    finally:
        channel.close()


def put(api: TransportPort, client: paramiko.SSHClient, path: str, content: str | bytes, mode: int = 0o600) -> None:
    with client.open_sftp() as sftp:
        with sftp.open(path, "wb") as f:
            f.write(content.encode() if isinstance(content, str) else content)
        sftp.chmod(path, mode)


def normalized_archive_entry(api: TransportPort, info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def upload_tree(api: TransportPort, client: paramiko.SSHClient, name: str = "app") -> None:
    if topology.services(api.ROOT)["artifact_delivery"] in ("s3", "release"):
        import artifact_transport

        return artifact_transport.source(api, client, name)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder in ["app", "config", "ansible", "scripts/remote"]:
            for path in (api.ROOT / folder).rglob("*"):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise ValueError("Source links are not allowed")
                relative = path.relative_to(api.ROOT)
                private = (
                    any(part.startswith(".") or part in ("__pycache__", "media") for part in relative.parts)
                    or path.suffix.lower() in (".pyc", ".pem", ".key", ".sqlite3", ".tfplan", ".env")
                    or ".tfstate" in path.name
                    or ".tfvars" in path.name
                    or path.name in ("operator.json", "settings.json", "secrets.json")
                )
                if path.is_file() and not private:
                    archive.add(
                        path,
                        arcname=path.relative_to(api.ROOT).as_posix(),
                        recursive=False,
                        filter=lambda info: api.normalized_archive_entry(info),
                    )
    target = api.remote_root
    api.put(client, target + ".tar.gz", buffer.getvalue())
    api.remote(
        client,
        "mkdir -p "
        + shlex.quote(target)
        + " && tar xzf "
        + shlex.quote(target + ".tar.gz")
        + " -C "
        + shlex.quote(target),
        "upload",
    )
