"""Prepare a clone-local provider mirror before allocating cloud resources."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from local_runtime import checked_tree
from runtime_ports import ProviderToolsPort


def cli_path(root: str | Path) -> Path:
    root = Path(root).resolve()
    local = root / ".tools/terraform-local.tfrc"
    checked_tree(local, root)
    return local if local.is_file() else root / "terraform-selectel.rc"


def prepare(api: ProviderToolsPort) -> Path:
    root = Path(api.ROOT).resolve()
    mirror = root / ".tools/terraform-providers"
    checked_tree(mirror, root)
    mirror.mkdir(parents=True, exist_ok=True)
    config = root / ".tools/terraform-local.tfrc"
    checked_tree(config, root)
    config.write_text(
        "provider_installation {\n  filesystem_mirror {\n    path = " + json.dumps(mirror.as_posix()) + "\n  }\n}\n",
        encoding="utf-8",
    )
    for kind in ("network", "global"):
        source = root / "terraform-selectel/staged" / kind
        with tempfile.TemporaryDirectory(prefix="provider-check-", dir=root / ".tools") as folder:
            work = Path(folder)
            for name in ("versions.tf", ".terraform.lock.hcl"):
                (work / name).write_bytes((source / name).read_bytes())
            env = {
                k: v for k, v in os.environ.items() if not k.startswith(("TF_", "OS_")) and k != "OTUS_SELECTEL_AUTH"
            }
            env.update(TF_IN_AUTOMATION="1", TF_WORKSPACE="default", TF_DATA_DIR=str(work / ".terraform"))
            command = [
                str(api.TF),
                "-chdir=" + folder,
                "init",
                "-backend=false",
                "-input=false",
                "-no-color",
                "-lockfile=readonly",
            ]
            env["TF_CLI_CONFIG_FILE"] = str(config)
            result = subprocess.run(command, env=env, capture_output=True, timeout=180)
            if result.returncode == 0:
                print("Terraform provider " + kind + ": local lock verification passed", flush=True)
                continue
            # Populate the mirror using Terraform's own lock verification. Never
            # retry apply or power operations; this operation only installs plugins.
            env.update(TF_CLI_CONFIG_FILE=str(root / "terraform-selectel.rc"), TF_PLUGIN_CACHE_DIR=str(mirror))
            for attempt in range(3):
                print("Terraform provider " + kind + ": download attempt " + str(attempt + 1), flush=True)
                result = subprocess.run(command, env=env, capture_output=True, timeout=180)
                if result.returncode == 0:
                    break
                output = (result.stdout + result.stderr).decode(errors="replace").lower()
                if not any(word in output for word in ("timeout", "timed out", "connection", "unexpected eof")):
                    break
            if result.returncode:
                from cloud_contract import TerraformError

                raise TerraformError(api.redact((result.stdout + result.stderr).decode(errors="replace")))
            # Recheck via exactly the offline configuration used by stage init.
            env.pop("TF_PLUGIN_CACHE_DIR", None)
            env["TF_CLI_CONFIG_FILE"] = str(config)
            result = subprocess.run(command, env=env, capture_output=True, timeout=180)
            if result.returncode:
                from cloud_contract import TerraformError

                raise TerraformError(api.redact((result.stdout + result.stderr).decode(errors="replace")))
            print("Terraform provider " + kind + ": downloaded and lock verified offline", flush=True)
    return config


if __name__ == "__main__":
    from pathlib import Path

    from runtime import create_runtime

    prepare(create_runtime(Path(__file__).resolve().parents[1]))
