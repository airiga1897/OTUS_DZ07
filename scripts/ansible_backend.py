"""Ansible operation boundary, without import-time configuration."""

from inventory_types import Inventory
from paramiko import SSHClient
from runtime_ports import OperationPort


def configure(api: OperationPort, app: SSHClient | None, inventory: Inventory) -> None:
    from ansible_operation import configure

    return configure(api, app, inventory)
