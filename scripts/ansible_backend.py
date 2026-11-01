"""DZ07 Ansible configuration runs independently of the operator's SSH session."""
from stack_config import load

STACK = load()
REMOTE = STACK['paths']['remote_root']
RUNTIME = REMOTE + '/.ansible-runtime'


def configure(api, app, inventory: dict) -> None:
    from ansible_operation import configure
    return configure(api, app, inventory)
