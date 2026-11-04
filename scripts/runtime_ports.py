"""Narrow contracts at state, transport and application boundaries."""

from collections.abc import Callable
from pathlib import Path
from tarfile import TarInfo
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, overload

from cloud_contract import ConfigurationTarget, InfrastructureResult
from cloud_contract import StandDescription as StandDescription
from cloud_settings import CloudIdentity
from credentials import AuthenticationSchema
from lifecycle_types import CycleReport
from paramiko import SSHClient
from selectel_adapter import SelectelAdapter
from stack_types import StackSettings
from topology import Topology

if TYPE_CHECKING:
    from selectel_staged import Backend


class PlacementIdentity(TypedDict, total=False):
    region: str | None
    availability_zone: str | None


class RootPort(Protocol):
    ROOT: Path


class DesiredTopologyPort(RootPort, Protocol):
    _TOPOLOGY_OVERRIDE: Topology | None


class FilePort(RootPort, Protocol):
    LOCAL: Path
    PREFIX: str

    def save(self, path: Path, value: object) -> None: ...
    def redact(self, text: str) -> str: ...


class CycleAccessPort(Protocol):
    LOCAL: Path
    _TEST_CYCLE_ID: str | None


class CycleProgressPort(FilePort, Protocol):
    _CYCLE_PROGRESS: CycleReport | None


class ProviderToolsPort(RootPort, Protocol):
    TF: Path

    def redact(self, text: str) -> str: ...


class TerraformPort(ProviderToolsPort, Protocol):
    RUN: Path


class TerraformExecutionPort(TerraformPort, Protocol):
    AUTH_SCHEMA: AuthenticationSchema
    CLOUD: CloudIdentity

    def auth_config(self) -> dict[str, str]: ...


class StagedControllerPort(FilePort, TerraformPort, Protocol):
    def desired_topology(self) -> Topology: ...


class RedactionPort(FilePort, Protocol):
    AUTH_SCHEMA: AuthenticationSchema
    _AUTH_CONFIG: dict[str, str] | None


class StoragePort(RedactionPort, Protocol):
    @property
    def load_credentials(self) -> Callable[[], dict[str, str]]: ...

    def placement(self) -> PlacementIdentity: ...
    def bind_context(self, config: dict[str, str]) -> None: ...


class DeliveryPort(Protocol):
    ROOT: Path
    LOCAL: Path
    STACK: StackSettings

    def put(self, client: SSHClient, path: str, content: str | bytes, mode: int = 0o600) -> None: ...
    def remote(
        self, client: SSHClient, command: str, label: str = "remote", stdin: str | bytes | None = None
    ) -> str: ...


class TransportPort(StagedControllerPort, DeliveryPort, Protocol):
    RUN: Path

    @property
    def remote_root(self) -> str: ...

    def cloud(self, bind: bool = True) -> SelectelAdapter: ...
    def normalized_archive_entry(self, info: TarInfo) -> TarInfo: ...


class OperationPort(FilePort, Protocol):
    STACK: StackSettings

    def lab_output(self) -> StandDescription: ...
    def connect(self, lab: StandDescription, name: str = "app", front: SSHClient | None = None) -> SSHClient: ...
    def remote(
        self, client: SSHClient, command: str, label: str = "remote", stdin: str | bytes | None = None
    ) -> str: ...


class ConfigurationTargetPort(Protocol):
    def configuration_target(self) -> ConfigurationTarget: ...


class ApplicationPort(OperationPort, StagedControllerPort, Protocol):
    def infrastructure_output(self) -> InfrastructureResult: ...
    def checked_plan(
        self, destroy: bool = False, allowed_new_nodes: set[str] | None = None, access_cidr: str | None = None
    ) -> Path: ...

    @overload
    def tf(self, *args: str, capture: Literal[True]) -> str: ...
    @overload
    def tf(self, *args: str, capture: Literal[False] = False) -> None: ...


class ServerDeliveryPort(OperationPort, DeliveryPort, Protocol):
    def upload_tree(self, client: SSHClient, name: str = "app") -> None: ...


class ConfigurationPort(ApplicationPort, ServerDeliveryPort, Protocol):
    def cloud(self, bind: bool = True) -> SelectelAdapter: ...
    def trust(self, lab: StandDescription) -> None: ...
    def discover_route(self, lab: StandDescription) -> None: ...
    def check_placement(self, lab: StandDescription) -> object: ...


class RetainedLifecyclePort(ConfigurationPort, StagedControllerPort, CycleAccessPort, Protocol):
    _STAGED_BACKEND: "Backend | None"

    def prepare_identity(self) -> Path: ...
    def configure(self, existing: bool = False) -> None: ...
