"""Explicit legacy regression fixture; its state is always inside a temporary clone."""

import datetime
import io
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import cloud_settings
import lab as cli
import paramiko
import stack_config
import topology
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from runtime import Runtime, create_runtime
from selectel_adapter import SelectelAdapter


@dataclass(repr=False)
class RuntimeFixture(Runtime):
    __file__: str = cli.__file__
    LOGGER: logging.Logger = field(default_factory=lambda: logging.getLogger("otus_dz07"))
    sys: ModuleType = sys
    subprocess: ModuleType = subprocess
    paramiko: ModuleType = paramiko
    datetime: ModuleType = datetime
    time: ModuleType = time
    io: ModuleType = io
    json: ModuleType = json
    os: ModuleType = os
    secrets: ModuleType = secrets
    tarfile: ModuleType = tarfile
    serialization: ModuleType = serialization
    topology: ModuleType = topology
    stack_config: ModuleType = stack_config
    cloud_settings: ModuleType = cloud_settings
    SelectelAdapter: type[SelectelAdapter] = SelectelAdapter
    Ed25519PrivateKey: type[Ed25519PrivateKey] = Ed25519PrivateKey

    def main(self) -> None:
        cli.main(self)


source = Path(__file__).resolve().parents[1]
temporary = tempfile.TemporaryDirectory(prefix="runtime-fixture-", dir=source / ".tools")
root = Path(temporary.name)
shutil.copytree(source / "config", root / "config")
shutil.copy(source / ".env.example", root / ".env.example")
(root / ".local").mkdir()
(root / "terraform-selectel").mkdir()
created = create_runtime(root)
lab = RuntimeFixture(
    ROOT=created.ROOT,
    LOCAL=created.LOCAL,
    STACK=created.STACK,
    AUTH_SCHEMA=created.AUTH_SCHEMA,
    CLOUD=created.CLOUD,
    PREFIX=created.PREFIX,
    TF=created.TF,
    RUN=created.RUN,
    load_credentials=created.load_credentials,
)
