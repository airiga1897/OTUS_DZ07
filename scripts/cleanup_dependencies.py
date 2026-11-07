"""Dependencies of retained Selectel stages, checked before deleting parents."""

from collections.abc import Container, Mapping


def _internal_pool(record: object, known: set[str]) -> str | None:
    if not isinstance(record, dict):
        return None
    candidate = record.get("candidate")
    if not isinstance(candidate, dict):
        return None
    region = candidate.get("region")
    return region if isinstance(region, str) and region in known else None


def children(stages: Mapping[str, object], nodes: Container[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {name: set() for name in stages}
    networks = {n for n in stages if n.startswith("network-")}
    connections = {n for n in stages if n.startswith("connection-")}
    known_pools = {name.split("-", 1)[1] for name in networks | connections}
    for name, record in stages.items():
        if name in nodes:
            pool = _internal_pool(record, known_pools)
            # Unknown placement must preserve every potential parent.
            parents = {p for p in networks | connections if not pool or p.split("-", 1)[1] == pool}
        elif name in connections:
            parents = {"global", "network-" + name.removeprefix("connection-")}
        elif name in networks or name == "global":
            parents = set()
        else:
            raise ValueError("Unknown cleanup stage; preserve recovery data")
        for parent in parents & result.keys():
            result[parent].add(name)
    return result
