"""Explicit runtime operations: application."""

import json
import secrets
import time

import pool_selection as pools
from cloud_settings import terraform_root
from progress_logging import get_logger
from runtime_ports import ApplicationPort, ConfigurationPort, StandDescription
from selectel_reports import NetworkVerification
from verification_types import VerificationReport

LOGGER = get_logger(__name__)


def lab_output(api: ApplicationPort) -> StandDescription:
    result = api.infrastructure_output().application()
    result["site_url"] = "http://" + result["app_public_ip"]
    return result


def check_placement(api: ConfigurationPort, lab: StandDescription) -> NetworkVerification | None:
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import backend

        result = backend(api).verify_placement()
        api.save(api.ROOT / "docs/network-result.json", result)
        return result
    import verification

    verification.check_placement(api, lab)
    return None


def configure(api: ConfigurationPort, existing: bool = False) -> None:
    import configuration

    return configuration.configure(api, existing)


def discover_route(api: ApplicationPort, lab: StandDescription) -> None:
    from stage_store import deployment_exists as exists

    if exists(api.ROOT):
        from staged_runtime import discover_route as stage_route

        return stage_route(api, lab)
    import urllib.request

    key = json.loads((api.LOCAL / "secrets.json").read_text())["api"].encode()
    nonce = secrets.token_hex(24)
    for _attempt in range(40):
        try:
            with urllib.request.urlopen(lab["site_url"] + "/?nonce=" + nonce, timeout=3) as response:
                result = json.load(response)
            from operator_network import signed_route_cidr

            cidr = signed_route_cidr(result, nonce, key)
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(2)
    else:
        raise RuntimeError("Direct HTTP route to application VM unavailable")
    path = terraform_root(api.ROOT) / "terraform.tfvars.json"
    variables = json.loads(path.read_text())
    if cidr not in variables["admin_cidrs"]:
        variables["admin_cidrs"].append(cidr)
        api.save(path, variables)
        plan = api.checked_plan(access_cidr=cidr)
        pools.phase(api.ROOT, "creating")
        api.tf("apply", "-input=false", "-no-color", str(plan))
    api.save(api.LOCAL / "operator-network.json", {"tcp_http_cidr": cidr, "admin_cidrs": variables["admin_cidrs"]})
    print("Direct route operator IPv4 verified; SSH remains restricted to /32 entries", flush=True)


def verify(api: ApplicationPort, failover: bool = False) -> VerificationReport:
    from elastic_options import require_no_failover

    require_no_failover(api)
    import verification

    return verification.verify(api, failover)


def diagnostics(api: ConfigurationPort) -> None:
    """Best-effort evidence; failures preserve resources and recovery files."""
    try:
        lab = api.lab_output()
        from network_diagnostics import console_evidence

        console_evidence(api, lab)
        with api.connect(lab) as app:
            api.remote(
                app,
                "sudo python3 -",
                "diagnostics-network-app",
                stdin=(api.ROOT / "scripts/remote/network_diagnostics.py").read_text(),
            )
            unit = f"postgresql@{api.STACK['postgresql']['major']}-main"
            api.remote(
                app,
                "sudo journalctl -u haproxy -u otus-dz07 -u nginx -u " + unit + " -n 150 --no-pager",
                "diagnostics-app",
            )
    except Exception:
        print("Additional diagnostics unavailable; existing run logs preserved", flush=True)
