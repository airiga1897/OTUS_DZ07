"""Local lifecycle operations; never authenticate or contact a cloud service."""

import ipaddress
import json
import shutil
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path

from cloud_settings import terraform_root
from selectel_api import json_contains, json_object, json_object_or_empty, json_rows, json_text, text_mapping


def read(path: Path) -> dict[str, object]:
    return json_object(json.loads(path.read_text(encoding="utf-8-sig")), "Unknown JSON object")


def _resources(document: Mapping[str, object]) -> list[dict[str, object]]:
    return json_rows(document.get("resources", []), "Unknown Terraform state")


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def checked_tree(path: Path, root: Path) -> None:
    if linked(path) or not path.resolve().is_relative_to(root):
        raise ValueError("Runtime path must stay inside this clone without links")
    if path.is_dir():
        for child in path.iterdir():
            checked_tree(child, root)


def cleanup_runtime(root: str | Path) -> None:
    """Call only after successful state AND cloud API deletion audits."""
    root = Path(root).resolve()
    local = root / ".local"
    staged = local / "staged-deployment.json"
    checked_tree(staged, root)
    from stage_store import require_context

    require_context(root)
    if staged.exists():
        journal = read(staged)
        stages = json_object(journal["stages"], "Unknown stage journal")
        if not journal.get("cleanup_verified") or any(
            json_object(stage, "Unknown stage journal")["status"] != "deleted" for stage in stages.values()
        ):
            raise ValueError("Sequential resources still need their recovery files")
        for path in (local / "stages").rglob("*.tfstate*"):
            checked_tree(path, root)
            if not path.name.endswith(".backup") and any(
                r.get("instances") for r in _resources(read(path)) if r.get("mode") == "managed"
            ):
                raise ValueError("Sequential managed state remains")
    state = terraform_root(root) / "terraform.tfstate"
    alternate = root / "terraform/terraform.tfstate"
    checked_tree(state, root)
    checked_tree(alternate, root)
    for other in root.glob("terraform*/terraform.tfstate*"):
        checked_tree(other, root)
        if other.parent != state.parent and any(
            r.get("mode") == "managed" and r.get("instances") for r in _resources(read(other))
        ):
            raise ValueError("Another provider state still needs local recovery files")
    if state.exists() and any(r.get("mode") == "managed" and r.get("instances") for r in _resources(read(state))):
        raise ValueError("Managed state remains; preserve recovery files")
    paths = (
        [
            p
            for p in local.iterdir()
            if p.name
            not in {
                "operator.json",
                "artifact-credentials.json",
                "artifact-storage.json",
                "lab-config.json",
                "stage-operation.lock",
            }
        ]
        if local.exists()
        else []
    )
    paths += list((terraform_root(root)).glob("terraform.tfstate*"))
    paths += list((terraform_root(root)).glob("*.tfplan"))
    paths += [
        p
        for p in (terraform_root(root)).glob("terraform.tfvars*")
        if p.name != "terraform.tfvars.example.json" and p.suffix == ".json"
    ]
    # Validate every target before removing any recovery file.
    checked_tree(local, root)
    for path in paths:
        checked_tree(path, root)
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def validate_state(root: str | Path, config: dict[str, str], prefix: str) -> None:
    """Fail closed on foreign project, pool, provider, or resource namespace."""
    state = terraform_root(Path(root)) / "terraform.tfstate"
    checked_tree(state, Path(root).resolve())
    if not state.exists():
        return
    from cloud_contract import validate_binding

    state_value = read(state)
    outputs = json_object(state_value.get("outputs") or {}, "Unknown Terraform state")
    infrastructure = json_object(outputs.get("infrastructure") or {}, "Unknown Terraform state")
    binding = infrastructure.get("value")
    if not binding and any(r.get("mode") == "managed" and r.get("instances") for r in _resources(state_value)):
        from cloud_settings import adapter

        binding = adapter().partial_state_binding(Path(root), config, prefix)
    if binding:
        from cloud_settings import adapter

        checked_binding = json_object(binding, "Unknown Terraform state")
        placement = json_object(checked_binding["placement"], "Unknown Terraform state")
        zone = placement.get("zone")
        zone_text = None if zone is None else json_text(zone, "Unknown Terraform state")
        expected = adapter().binding(config, prefix, zone_text)
        validate_binding(checked_binding, expected)
        binding = checked_binding
        context_path = Path(root) / ".local/context.json"
        if context_path.exists():
            validate_binding(read(context_path), binding)
    from cloud_settings import adapter

    adapter().validate_managed_state(state_value, config, prefix)


def _node_ids(nodes: Mapping[str, object], message: str) -> dict[str, str]:
    return {name: json_text(json_object(node, message)["id"], message) for name, node in nodes.items()}


def _portal_address(document: Mapping[str, object], homework: int) -> str:
    if homework == 7:
        return json_text(json_object(document["es3"], "Unknown portal output")["public_ip"], "Unknown portal output")
    return json_text(document["app_public_ip"], "Unknown portal output")


def open_portal(root: str | Path, homework: int, opener: Callable[[str], object] | None = None) -> str:
    """Validate saved identities against local state; no cloud credentials needed."""
    root = Path(root)
    local = root / ".local"
    try:
        checked_tree(terraform_root(root) / "terraform.tfstate", root.resolve())
        checked_tree(
            local / {5: "deployment.json", 6: "context.json", 7: "selectel-context.json"}[homework], root.resolve()
        )
        checked_tree(local / "lab.json", root.resolve())
        state = read(terraform_root(root) / "terraform.tfstate")
        resources = [
            resource
            for resource in json_rows(state.get("resources", []), "Unknown Terraform state")
            if resource.get("mode") == "managed" and resource.get("instances")
        ]
        if not resources:
            raise ValueError("No active managed state")
        context = read(local / {5: "deployment.json", 6: "context.json", 7: "selectel-context.json"}[homework])
        if homework == 6:
            from cloud_contract import InfrastructureResult, validate_binding

            outputs = json_object(state["outputs"], "Unknown Terraform state")
            infrastructure = json_object(outputs["infrastructure"], "Unknown Terraform state")
            result = InfrastructureResult.from_dict(infrastructure["value"])
            validate_binding(context, result.binding())
            context = {
                **context,
                "project_id": result.scope["project_id"],
                "region": json_text(result.placement.get("region"), "Unknown Terraform state"),
                "availability_zone": json_text(result.placement.get("zone"), "Unknown Terraform state"),
            }
        if homework == 5 and context.get("phase") != "active":
            raise ValueError("Deployment is not active")
        if context.get("state_lineage") and context["state_lineage"] != state.get("lineage"):
            raise ValueError("Stale deployment lineage")
        outputs = json_object(state["outputs"], "Unknown Terraform state")
        output = json_object(json_object(outputs["lab"], "Unknown Terraform state")["value"], "Unknown Terraform state")
        saved = read(local / "lab.json") if homework != 6 else output
        if homework == 5 and saved.get("deployment_id") != context.get("deployment_id"):
            raise ValueError("Stale deployment identity")
        fallback = json_object_or_empty(context.get("config"), "Unknown portal context")
        prefix = json_text(
            context.get("namespace", fallback.get("name_prefix", f"otus-dz0{homework}")),
            "Unknown portal context",
        )
        ids: dict[str, str] = {}
        for resource in resources:
            for item in json_rows(resource["instances"], "Unknown Terraform state"):
                attributes = json_object(item.get("attributes", {}), "Unknown Terraform state")
                if attributes.get("tenant_id") and attributes["tenant_id"] != context["project_id"]:
                    raise ValueError("Foreign project in state")
                if attributes.get("region") and attributes["region"] != context["region"]:
                    raise ValueError("Foreign region in state")
                name = attributes.get("name")
                if (
                    name
                    and name != prefix
                    and not json_text(name, "Foreign resource in state").startswith(prefix + "-")
                ):
                    raise ValueError("Foreign resource in state")
                if resource.get("type") == "openstack_compute_instance_v2":
                    ids[json_text(item["index_key"], "Stale VM identities")] = json_text(
                        attributes["id"], "Stale VM identities"
                    )
        if homework == 7:
            expected = _node_ids(saved, "Unknown portal output")
            output_ids = _node_ids(output, "Unknown portal output")
        else:
            expected = text_mapping(saved["server_ids"], "Unknown portal output")
            output_ids = text_mapping(output["server_ids"], "Unknown portal output")
        if not ids or ids != expected or ids != output_ids:
            raise ValueError("Stale VM identities")
        address = _portal_address(saved, homework)
        current_address = _portal_address(output, homework)
        if address != current_address:
            raise ValueError("Stale portal address")
        ipaddress.IPv4Address(address)
        url = "http://" + address
        region = json_text(context["region"], "Unknown portal context")
        zone_value = context.get("availability_zone")
        if not zone_value:
            variables = read(terraform_root(root) / "terraform.tfvars.json")
            zone = json_text(variables["availability_zone"], "Unknown portal context")
        else:
            zone = json_text(zone_value, "Unknown portal context")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "Нет действующего локального контекста портала: проверьте context, state и завершение deploy; сохраните данные для восстановления."
        ) from error
    print(f"Placement: {region}/{zone}\nPortal: {url}", flush=True)
    if json_contains(saved.get("server_ids"), "kibana", "Unknown portal output"):
        from service_config import kibana_port

        print("Kibana: " + url + ":" + str(kibana_port(root)), flush=True)
    if not (opener or webbrowser.open)(url):
        print("Браузер не открылся автоматически; используйте URL выше.", flush=True)
    return url
