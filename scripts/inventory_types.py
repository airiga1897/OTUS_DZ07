"""The Ansible inventory shape shared by rendering and the operation controller."""

from __future__ import annotations

from typing import TypedDict


class InventoryGroup(TypedDict, total=False):
    vars: dict[str, object]
    hosts: dict[str, dict[str, object]]
    children: dict[str, InventoryGroup]


class Inventory(TypedDict):
    all: InventoryGroup
