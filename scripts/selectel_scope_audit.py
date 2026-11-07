"""Bounded read-only namespace audits; unknown coverage never means absence."""

import json
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from local_runtime import checked_tree
from progress_logging import get_logger, phase
from selectel_api import json_rows
from selectel_reports import PoolAudit

if TYPE_CHECKING:
    from runtime_ports import FilePort
    from selectel_adapter import SelectelAdapter

LOGGER = get_logger(__name__)


def workers(root: Path) -> int:
    path = Path(root) / "config/cloud.json"
    value = json.loads(path.read_text()).get("audit", {}).get("read_workers", 1) if path.exists() else 1
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError("Invalid audit read worker limit")
    return value


def read_independent[Result](tasks: Sequence[tuple[str, Callable[[], Result]]], limit: int) -> dict[str, Result]:
    """Finish all reads, then raise the first original error in deterministic order."""
    if not tasks:
        return {}
    results: dict[str, Result] = {}
    failures: list[tuple[str, Exception]] = []
    with ThreadPoolExecutor(max_workers=min(limit, len(tasks))) as executor:
        futures = [(name, executor.submit(function)) for name, function in tasks]
        for name, future in futures:
            try:
                results[name] = future.result()
            except Exception as error:
                failures.append((name, error))
    if failures:
        original_error = failures[0][1]
        original_error.__dict__["audit_incomplete"] = {name: type(failure).__name__ for name, failure in failures}
        raise original_error
    return results


def saved_scope(api: "FilePort", project: str) -> list[str] | None:
    regions: set[str] = set()
    uncertain = False
    for filename in ("context.json", "selectel-context.json", "infrastructure.json"):
        path = api.LOCAL / filename
        checked_tree(path, Path(api.ROOT).resolve())
        if not path.exists():
            continue
        saved = json.loads(path.read_text())
        if saved.get("project_id", saved.get("scope", {}).get("project_id")) != project:
            raise ValueError("Existing local context has a different or unknown project")
        if saved.get("namespace", api.PREFIX) != api.PREFIX or saved.get("provider", "selectel") != "selectel":
            raise ValueError("Existing local context belongs to another namespace or provider")
        placement = saved.get("placement", {})
        found = [saved.get("region"), placement.get("region")]
        found += [n.get("region") for n in saved.get("nodes", {}).values()]
        found += [n.get("region") for n in placement.get("node_placements", {}).values()]
        found = [r for r in found if r is not None]
        if any(not isinstance(r, str) or not r for r in found):
            raise ValueError("Unknown pool in saved audit scope")
        if not found or saved.get("namespace") != api.PREFIX:
            uncertain = True
        regions.update(found)
    for path in Path(api.ROOT).glob("terraform*/terraform.tfstate*"):
        checked_tree(path, Path(api.ROOT).resolve())
        value = json.loads(path.read_text())
        if any(r.get("instances") for r in value.get("resources", []) if r.get("mode") == "managed"):
            raise ValueError("Non-empty state requires its saved recovery workflow")
    return sorted(regions) if regions and not uncertain else None


def audit_pool(
    api: "FilePort",
    regional: "SelectelAdapter",
    pool: str,
    reject: "Callable[[FilePort, SelectelAdapter], None]",
    excluded_services: Collection[str] = (),
) -> PoolAudit:
    with phase(LOGGER, "Аудит пространства имён проекта", pool=pool):
        reject(api, regional)
        if "network" in regional.endpoints:
            import urllib.error

            from selectel_staged_audit import objects

            try:
                floating = objects(regional, "network", "/v2.0/floatingips", "floatingips")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                aliases = {
                    e["alias"]
                    for e in json_rows(
                        regional.get("network", "/v2.0/extensions")["extensions"],
                        "Unknown network extension payload",
                    )
                }
                if "router" in aliases:
                    raise
                floating = []
            if any(
                isinstance(description := f.get("description"), str) and description.startswith(api.PREFIX + "-")
                for f in floating
            ):
                raise RuntimeError("Untracked deployment floating IP remains in " + pool)
        missing = {"compute", "volumev3", "network"} - set(regional.endpoints)
        excluded = missing & set(excluded_services)
        if excluded:
            LOGGER.info(
                "Аудит [пул %s]: проверка %s исключена явной политикой адаптера; отсутствие ресурсов этого типа не подтверждается",
                pool,
                ", ".join(sorted(excluded)),
            )
        missing -= excluded
        if missing:
            raise ValueError(
                "Incomplete resource audit in " + pool + ": APIs unavailable: " + ", ".join(sorted(missing))
            )
        return {"excluded_services": sorted(excluded)}
