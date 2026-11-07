"""Inventory for standalone PostgreSQL on app and dedicated Elasticsearch nodes."""

from cloud_contract import InfrastructureResult
from inventory_types import Inventory
from stack_config import load
from stack_types import StackSettings


def _application_inventory(
    infrastructure: InfrastructureResult, passwords: dict[str, str], stack: StackSettings | None = None
) -> Inventory:
    stack = load() if stack is None else stack
    runtime = stack["paths"]["remote_root"] + "/.ansible-runtime"
    infrastructure.validate()
    lab = infrastructure.application()
    return {
        "all": {
            "vars": {
                "stack": stack,
                "ansible_user": "otus",
                "ansible_port": 22,
                "ansible_python_interpreter": "/usr/bin/python3",
                "ansible_ssh_private_key_file": runtime + "/key",
                "ansible_ssh_common_args": "-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes "
                "-o UserKnownHostsFile=" + runtime + "/known_hosts",
            },
            "children": {
                "database": {"hosts": {"app": {"postgres_password": passwords["postgres"]}}},
                "frontend": {
                    "hosts": {
                        "app": {
                            "ansible_connection": "local",
                            "portal_env": {
                                "DJANGO_SECRET_KEY": passwords["django"],
                                "DB_NAME": "otus",
                                "DB_USER": "otus",
                                "DB_PASSWORD": passwords["app"],
                                "DB_HOST": "127.0.0.1",
                                "DB_PORT": str(stack["postgresql"]["port"]),
                                "DJANGO_ALLOWED_HOSTS": lab["app_public_ip"] + ",localhost,127.0.0.1",
                                "REQUIRE_SHARED_MOUNT": "0",
                                "SHARED_ROOT": stack["paths"]["shared"],
                                "NODE_NAME": stack["name_prefix"] + "-app",
                            },
                        }
                    }
                },
            },
        }
    }


def ansible_inventory(
    infrastructure: InfrastructureResult, passwords: dict[str, str], stack: StackSettings | None = None
) -> Inventory:
    from observability import inventory

    value = _application_inventory(infrastructure, passwords, stack)
    return inventory(infrastructure, passwords, value)
