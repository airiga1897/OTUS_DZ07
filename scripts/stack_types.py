"""Types of this project's non-secret stack configuration."""

from typing import TypedDict


class PostgreSQLSettings(TypedDict):
    major: int
    port: int
    data_dir: str


class PortalSettings(TypedDict):
    uwsgi_version: str
    service_user: str
    upstream_port: int


class StackSettings(TypedDict):
    name_prefix: str
    subnet_cidr: str
    paths: dict[str, str]
    postgresql: PostgreSQLSettings
    ansible: dict[str, str]
    portal: PortalSettings
    web: dict[str, int]
