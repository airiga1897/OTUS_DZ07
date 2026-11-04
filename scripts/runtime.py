"""Composition root: construction is explicit and performs no cloud operations."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tarfile import TarInfo
from typing import TYPE_CHECKING, Literal, overload

import cloud_settings
import pool_selection as pools
import runtime_application
import runtime_infrastructure
import runtime_lifecycle
import runtime_ssh
import runtime_storage
import stack_config
from cloud_contract import InfrastructureResult
from credentials import AuthenticationSchema, load_credentials
from credentials import schema as load_authentication_schema
from lifecycle_types import CycleReport
from paramiko import SSHClient
from placement_config import PlacementPolicy
from runtime_ports import StandDescription
from selectel_adapter import SelectelAdapter
from selectel_reports import NetworkVerification
from stack_types import StackSettings
from topology import Topology
from verification_types import VerificationReport

if TYPE_CHECKING:
    from selectel_staged import Backend


@dataclass(repr=False)
class Runtime:
    pools = pools
    ROOT: Path
    LOCAL: Path
    STACK: StackSettings
    AUTH_SCHEMA: AuthenticationSchema
    CLOUD: cloud_settings.CloudIdentity
    PREFIX: str
    TF: Path
    RUN: Path
    load_credentials: Callable[[], dict[str, str]] = field(default=load_credentials, repr=False)
    _AUTH_CONFIG: dict[str, str] | None = None
    _TEST_CYCLE_ID: str | None = None
    _TOPOLOGY_OVERRIDE: Topology | None = None
    _STAGED_BACKEND: "Backend | None" = None
    _CYCLE_PROGRESS: CycleReport | None = None

    @property
    def remote_root(self) -> str:
        value = self.STACK["paths"]["remote_root"]
        if not isinstance(value, str):
            raise ValueError("Remote root must be a string")
        return value

    def cloud(self, bind: bool = True) -> SelectelAdapter:
        return SelectelAdapter(self.auth_config() if bind else self.load_credentials())

    def save(self, path: Path, value: object) -> None:
        return runtime_storage.save(self, path, value)

    def redact(self, text: str) -> str:
        return runtime_storage.redact(self, text)

    def bind_context(self, config: dict[str, str]) -> None:
        return runtime_storage.bind_context(self, config)

    def auth_config(self) -> dict[str, str]:
        return runtime_storage.auth_config(self)

    def prepare_identity(self) -> Path:
        return runtime_storage.prepare_identity(self)

    @overload
    def tf(self, *args: str, capture: Literal[True]) -> str: ...
    @overload
    def tf(self, *args: str, capture: Literal[False] = False) -> None: ...
    @overload
    def tf(self, *args: str, capture: bool) -> str | None: ...

    def tf(self, *args: str, capture: bool = False) -> str | None:
        return runtime_infrastructure.tf(self, *args, capture=capture)

    def placement(self) -> PlacementPolicy:
        return runtime_infrastructure.placement(self)

    def desired_topology(self) -> Topology:
        return runtime_infrastructure.desired_topology(self)

    def preflight(self) -> None:
        return runtime_infrastructure.preflight(self)

    def prepare(self) -> None:
        return runtime_infrastructure.prepare(self)

    def checked_plan(self, destroy=False, allowed_new_nodes=None, access_cidr=None):
        return runtime_infrastructure.checked_plan(self, destroy, allowed_new_nodes, access_cidr)

    def infrastructure_output(self) -> InfrastructureResult:
        return runtime_infrastructure.infrastructure_output(self)

    def managed_ids(self):
        return runtime_infrastructure.managed_ids(self)

    def cleanup(self, clean_local: bool = True) -> None:
        return runtime_infrastructure.cleanup(self, clean_local)

    def select_candidate(self, choice: dict[str, object]) -> None:
        return runtime_infrastructure.select_candidate(self, choice)

    def deployment_candidates(self):
        return runtime_infrastructure.deployment_candidates(self)

    def prepare_selected(self) -> None:
        return runtime_infrastructure.prepare_selected(self)

    def trust(self, lab: StandDescription) -> None:
        return runtime_ssh.trust(self, lab)

    def connect(self, lab: StandDescription, name: str = "app", front: SSHClient | None = None) -> SSHClient:
        return runtime_ssh.connect(self, lab, name, front)

    def remote(self, client: SSHClient, command: str, label: str = "remote", stdin: str | bytes | None = None) -> str:
        return runtime_ssh.remote(self, client, command, label, stdin)

    def put(self, client: SSHClient, path: str, content: str | bytes, mode: int = 0o600) -> None:
        return runtime_ssh.put(self, client, path, content, mode)

    def normalized_archive_entry(self, info: TarInfo) -> TarInfo:
        return runtime_ssh.normalized_archive_entry(self, info)

    def upload_tree(self, client: SSHClient, name: str = "app") -> None:
        return runtime_ssh.upload_tree(self, client, name)

    def lab_output(self) -> StandDescription:
        return runtime_application.lab_output(self)

    def check_placement(self, lab: StandDescription) -> NetworkVerification | None:
        return runtime_application.check_placement(self, lab)

    def configure(self, existing: bool = False) -> None:
        return runtime_application.configure(self, existing)

    def discover_route(self, lab: StandDescription) -> None:
        return runtime_application.discover_route(self, lab)

    def verify(self, failover: bool = False) -> VerificationReport:
        return runtime_application.verify(self, failover)

    def diagnostics(self):
        return runtime_application.diagnostics(self)

    def deploy(self, *, initial_creation: bool = False) -> None:
        return runtime_lifecycle.deploy(self, initial_creation=initial_creation)

    def deploy_selected(self) -> None:
        return runtime_lifecycle.deploy_selected(self)

    def regression(self):
        return runtime_lifecycle.regression(self)

    def destroy(self, purge_artifacts: bool = False) -> None:
        return runtime_lifecycle.destroy(self, purge_artifacts)


def create_runtime(root: Path) -> Runtime:
    stack = stack_config.load(root)
    cloud = cloud_settings.load(root)
    local = root / ".local"
    run = local / "runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Runtime(
        ROOT=root,
        LOCAL=local,
        STACK=stack,
        AUTH_SCHEMA=load_authentication_schema(root),
        CLOUD=cloud,
        PREFIX=stack["name_prefix"],
        TF=root / ".tools/terraform.exe",
        RUN=run,
        load_credentials=partial(load_credentials, root),
    )
