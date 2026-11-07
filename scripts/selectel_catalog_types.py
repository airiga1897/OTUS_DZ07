"""Consumed shapes of Selectel/OpenStack placement catalogs, without live state."""

from typing import NotRequired, TypedDict, cast


class Flavor(TypedDict):
    id: str
    name: str
    vcpus: NotRequired[object]
    ram: NotRequired[object]
    disk: NotRequired[object]
    availability_zones: NotRequired[object]
    # This API key intentionally retains its provider spelling.
    # Public access is read with get() because unsupported values reject a flavor.


class VolumeType(TypedDict):
    name: str
    id: NotRequired[object]
    extra_specs: NotRequired[object]


class ZoneState(TypedDict):
    available: bool


class ComputeZone(TypedDict):
    zoneName: str
    zoneState: ZoneState


class NetworkZone(TypedDict):
    name: str
    resource: str
    state: str


class CatalogResponse(TypedDict, total=False):
    flavors: list[Flavor]
    volume_types: list[VolumeType]
    availabilityZoneInfo: list[ComputeZone]
    availability_zones: list[NetworkZone]
    extra_specs: object


def document(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Unknown placement catalog object")
    return cast(dict[str, object], value)


def rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Unknown placement catalog collection")
    return [document(row) for row in value]


def text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Unknown placement catalog text field")
    return value


def flavor_rows(value: object) -> list[Flavor]:
    result = rows(value)
    for row in result:
        text(row.get("id"))
        text(row.get("name"))
    return cast(list[Flavor], result)


def volume_rows(value: object) -> list[VolumeType]:
    result = rows(value)
    for row in result:
        text(row.get("name"))
    return cast(list[VolumeType], result)


def compute_zones(value: object) -> list[ComputeZone]:
    result = rows(value)
    for row in result:
        text(row.get("zoneName"))
        if type(document(row.get("zoneState")).get("available")) is not bool:
            raise ValueError("Unknown placement catalog zone availability")
    return cast(list[ComputeZone], result)


def network_zones(value: object) -> list[NetworkZone]:
    result = rows(value)
    for row in result:
        for name in ("name", "resource", "state"):
            text(row.get(name))
    return cast(list[NetworkZone], result)


def checked(value: object) -> CatalogResponse:
    result = document(value)
    if "flavors" in result:
        flavor_rows(result["flavors"])
    if "volume_types" in result:
        volume_rows(result["volume_types"])
    if "availabilityZoneInfo" in result:
        compute_zones(result["availabilityZoneInfo"])
    if "availability_zones" in result:
        network_zones(result["availability_zones"])
    return cast(CatalogResponse, result)
