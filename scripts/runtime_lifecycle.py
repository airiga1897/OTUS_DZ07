"""Explicit runtime operations: lifecycle."""

import json
from typing import TYPE_CHECKING

import pool_selection as pools
import stack_config
import topology
from cloud_settings import terraform_root
from network_diagnostics import BootstrapNetworkError
from progress_logging import get_logger

if TYPE_CHECKING:
    from runtime import Runtime

LOGGER = get_logger(__name__)


def deploy(api: "Runtime", *, initial_creation: bool = False) -> None:
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import deploy as deploy_stages

        return deploy_stages(api)
    from test_cycle import require_access

    require_access(api)
    existing = api.managed_ids()
    saved = pools.selected(api.ROOT)
    configured = bool(existing) and (
        (api.LOCAL / "configured.json").exists()
        or (api.LOCAL / "expansion-pending.json").exists()
        or not saved
        or saved.get("phase") == "active"
    )
    try:
        import infrastructure
        import verification

        desired = api.desired_topology()
        configured_path = api.LOCAL / "configured.json"
        if configured_path.exists():
            saved_config = json.loads(configured_path.read_text())
            if saved_config.get("stack_sha256", stack_config.digest(api.STACK)) != stack_config.digest(api.STACK):
                raise ValueError("Changing stack settings on an existing stand requires a separate migration")
        snapshot = None
        if configured:
            previous = api.lab_output()
            topology.validate_extension(topology.from_lab(previous), desired)
            snapshot = verification.snapshot(api, previous)
        added = None
        if snapshot:
            added = set(desired["nodes"]) - set(snapshot["server_ids"])
        infrastructure.apply(api, desired, allowed_new_nodes=added)
        topology.assert_preserved(existing, api.managed_ids())
        api.configure(existing=configured)
        api.save(
            api.LOCAL / "configured.json", {"portal_configured": True, "stack_sha256": stack_config.digest(api.STACK)}
        )
        if snapshot:
            verification.expansion(api, snapshot)
        pools.phase(api.ROOT, "active")
    except BaseException as error:
        from placement_diagnostics import preserve

        preserve(api, error)
        if (
            not existing
            and not configured
            and (initial_creation or api._TEST_CYCLE_ID)
            and not (api.LOCAL / "configured.json").exists()
            and not (api.LOCAL / "expansion-pending.json").exists()
            and (
                isinstance(error, BootstrapNetworkError)
                or pools.confirmed_capacity(error, api.ROOT, api.cloud, api.PREFIX)
            )
        ):
            reason = (
                "guest_IPv4_bootstrap_failed"
                if isinstance(error, BootstrapNetworkError)
                else "scheduler_capacity_unavailable"
            )
            api.save(
                api.ROOT / "docs/capacity-result.json",
                {
                    "reason": reason,
                    "region": api.placement()["region"],
                    "availability_zone": api.placement()["availability_zone"],
                },
            )
            print("Initial placement failed: " + reason + "; cleaning before the next candidate", flush=True)
            api.cleanup(clean_local=False)
            raise pools.CapacityRetry() from error
        api.diagnostics()
        print("DZ07 state, resources and recovery files preserved after deployment error", flush=True)
        raise


def deploy_selected(api: "Runtime") -> None:
    from stage_store import require_context

    require_context(api.ROOT)
    from cloud_settings import sequential_enabled as enabled
    from stage_store import deployment_exists as exists

    if exists(api.ROOT) or (enabled(api.ROOT) and not api.managed_ids()):
        from staged_runtime import deploy as deploy_stages

        return deploy_stages(api)
    from test_cycle import require_access

    require_access(api)
    if api.managed_ids() or (
        pools.selected(api.ROOT) and (terraform_root(api.ROOT) / "terraform.tfvars.json").exists()
    ):
        api.deploy()  # Keep the saved placement for repeat configuration.
        return
    if (api.LOCAL / "configured.json").exists() or (api.LOCAL / "expansion-pending.json").exists():
        raise ValueError("Existing configuration requires recovery; refusing initial placement retry")
    pools.run_candidates(
        api.deployment_candidates(), api.select_candidate, api.prepare, lambda: api.deploy(initial_creation=True)
    )


def regression(api: "Runtime") -> None:
    from test_cycle import run

    return run(api)


def destroy(api: "Runtime", purge_artifacts: bool = False) -> None:
    api.cleanup()
    if purge_artifacts:
        from artifact_lifecycle import purge

        purge(api.ROOT)
