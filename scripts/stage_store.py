"""Private Terraform state per deployment stage, with checked plans and paths."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import logging

from local_runtime import checked_tree
from cloud_contract import TerraformError

JOURNAL = 'staged-deployment.json'
_held = threading.local()
LOGGER = logging.getLogger(__name__)



def deployment_exists(root: Path) -> bool:
    return (Path(root) / '.local' / JOURNAL).exists()


def require_context(root: Path) -> None:
    stages = Path(root) / '.local/stages'
    checked_tree(stages, Path(root).resolve())
    if not deployment_exists(root) and stages.exists() and any(stages.iterdir()):
        raise ValueError('Stage context is missing; preserve all stage states for explicit recovery')


def atomic_json(path: Path, value: dict, root: Path) -> None:
    checked_tree(path, root.resolve())
    temporary = path.with_name(path.name + '.next')
    checked_tree(temporary, root.resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resources(module: dict) -> list[dict]:
    result = [r for r in module.get('resources', []) if r.get('mode') == 'managed']
    for child in module.get('child_modules', []):
        result.extend(resources(child))
    return result


def check_plan(plan: dict, before: dict, destroy: bool = False, additions_only: bool = False, allow_drift=None) -> None:
    for drift in plan.get('resource_drift', []):
        if drift['change']['actions'] != ['no-op']:
            allowed_deletion = (destroy and drift['change']['actions'] == ['delete']
                and drift['address'] in before and (drift['change'].get('before') or {}).get('id') == before[drift['address']])
            change = drift['change']
            allowed_refresh = (change['actions'] == ['update'] and drift['address'] in before
                and (change.get('before') or {}).get('id') == before[drift['address']]
                and (change.get('after') or {}).get('id') == before[drift['address']]
                and allow_drift is not None and allow_drift(drift) is True)
            if not allowed_deletion and not allowed_refresh:
                raise ValueError('Resource drift requires explicit recovery: ' + drift['address'])
    for change in plan.get('resource_changes', []):
        if change.get('mode') == 'data':
            continue
        actions = change['change']['actions']
        if actions not in (['no-op'], ['delete'] if destroy else ['create']):
            raise ValueError('Unsafe stage plan: ' + change['address'])
        if change['address'] in before:
            if (change['change'].get('before') or {}).get('id') != before[change['address']]:
                raise ValueError('Existing stage identity changed')
            if additions_only and actions != ['no-op']:
                raise ValueError('Existing resources must remain unchanged')
        elif actions == ['delete']:
            raise ValueError('Deletion outside the saved stage state')


@contextmanager
def operation(local: Path):
    """An OS lock is automatically released if the controller is interrupted."""
    local.mkdir(parents=True, exist_ok=True)
    path = local / 'stage-operation.lock'
    checked_tree(path, local.parent.resolve())
    held = getattr(_held, 'paths', {})
    _held.paths = held
    identity = str(path.resolve())
    if identity in held:
        held[identity] += 1
        try:
            yield
        finally:
            held[identity] -= 1
        return
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held[identity] = 1
        try:
            yield
        finally:
            held.pop(identity)
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class StageStore:
    def __init__(self, api, journal: dict, environment=None, cli_config=None, drift_validator=None):
        self.api, self.journal = api, journal
        self.environment, self.cli_config = environment, cli_config
        self.drift_validator = drift_validator
        self.root = Path(api.ROOT).resolve()
        self.local = self.root / '.local/stages'

    def folder(self, name: str) -> Path:
        if not re.fullmatch('[a-z][a-z0-9-]*', name):
            raise ValueError('Invalid stage name')
        path = self.local / name
        checked_tree(path, self.root)
        return path

    def state(self, name: str) -> dict:
        path = self.folder(name) / 'terraform.tfstate'
        checked_tree(path, self.root)
        return json.loads(path.read_text()) if path.exists() else {}

    def entries(self, name: str) -> list[dict]:
        return [dict(type=r['type'], address=r.get('module', '') + '.' + r['type'] + '.' + r['name'],
                     attrs=i.get('attributes', {}), index=i.get('index_key'))
                for r in self.state(name).get('resources', []) if r.get('mode') == 'managed'
                for i in r.get('instances', [])]

    def identities(self, name: str) -> dict:
        # Use Terraform's full addresses, including module and index, in the plan guard.
        result = {}
        for r in self.state(name).get('resources', []):
            if r.get('mode') != 'managed':
                continue
            for item in r.get('instances', []):
                address = (r.get('module', '') + '.' if r.get('module') else '') + r['type'] + '.' + r['name']
                if 'index_key' in item:
                    address += '[' + json.dumps(item['index_key']) + ']'
                result[address] = item['attributes']['id']
        return result

    def configure(self, name: str, source: Path, inputs: dict, providers=None) -> None:
        folder = self.folder(name)
        folder.mkdir(parents=True, exist_ok=True)
        checked_tree(source, self.root)
        if not source.is_dir():
            raise ValueError('Provider stage module is missing')
        lock = source / '.terraform.lock.hcl'
        if lock.exists() and not (folder / lock.name).exists():
            (folder / lock.name).write_bytes(lock.read_bytes())
        relative = os.path.relpath(source, folder).replace('\\', '/')
        config = {'module': {'stage': {'source': relative, **inputs}},
                  'output': {'result': {'value': '${module.stage.result}'}}}
        if providers:
            config['terraform'] = {'required_providers': providers}
        atomic_json(folder / 'main.tf.json', config, self.root)

    def tf(self, name: str, *args) -> str:
        if self.environment is None or self.cli_config is None:
            raise ValueError('Provider Terraform environment is not configured')
        env = dict(self.environment(self.journal['stages'][name]['candidate']))
        for key in list(env):
            if key.startswith(('TF_VAR_', 'TF_CLI_ARGS')) or key in {'TF_LOG', 'TF_LOG_CORE', 'TF_LOG_PROVIDER', 'TF_LOG_PATH', 'TF_PLUGIN_CACHE_DIR', 'TF_WORKSPACE', 'TF_DATA_DIR'}:
                env.pop(key, None)
        env.update(TF_CLI_CONFIG_FILE=str(self.cli_config), TF_IN_AUTOMATION='1',
                   TF_WORKSPACE='default', TF_DATA_DIR=str(self.folder(name) / '.terraform'))
        result = subprocess.run([str(self.api.TF), '-chdir=' + str(self.folder(name)), *args],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = self.api.redact((result.stdout + result.stderr).decode(errors='replace'))
        error = None
        if result.returncode:
            error = TerraformError(output)
            error.terraform_stage, error.terraform_operation = name, args[0]
            try:
                from placement_diagnostics import capture_terraform
                error.failure_evidence = capture_terraform(self.api, name, args[0],
                    self.journal['stages'][name]['candidate'], output, result.returncode)
            except (OSError, ValueError, KeyError) as diagnostic_error:
                error.diagnostics_error_type = type(diagnostic_error).__name__
                LOGGER.error('Cannot preserve Terraform failure (%s); original failure and cleanup retained',
                             type(diagnostic_error).__name__)
        try:
            self.api.RUN.mkdir(parents=True, exist_ok=True)
            with (self.api.RUN / ('stage-' + name + '-' + args[0] + '.log')).open('a', encoding='utf-8') as log:
                log.write('\n[Terraform invocation]\n' + output)
        except OSError:
            if error is None:
                raise
            LOGGER.error('Cannot write private Terraform log; original failure retained')
        if error is not None:
            raise error
        return result.stdout.decode()

    def apply(self, name: str, destroy: bool = False, unchanged: bool = False, validate_extra=None) -> dict:
        self.tf(name, 'init', '-input=false', '-no-color')
        before = self.identities(name)
        args = ['plan', '-input=false', '-no-color', '-out=stage.tfplan']
        if destroy:
            args.append('-destroy')
        self.tf(name, *args)
        plan = json.loads(self.tf(name, 'show', '-json', 'stage.tfplan'))
        try:
            check_plan(plan, before, destroy, additions_only=not destroy,
                       allow_drift=(lambda drift: self.drift_validator(name, drift)) if self.drift_validator else None)
            if unchanged and any(change.get('mode') != 'data' and change['change']['actions'] != ['no-op'] for change in plan.get('resource_changes', [])):
                raise ValueError('An active stage may not add or alter infrastructure resources')
            if validate_extra is not None:
                validate_extra(plan)
        except ValueError as error:
            error.terraform_stage, error.terraform_operation = name, 'plan-guard'
            try:
                from plan_diagnostics import summarize
                summary = summarize(plan, self.journal.get('stages', {}).get(name, {}).get('inputs', {}))
                from placement_diagnostics import capture_terraform
                error.failure_evidence = capture_terraform(self.api, name, 'plan-guard',
                    self.journal.get('stages', {}).get(name, {}).get('candidate', {}), json.dumps(summary), 0)
            except (OSError, ValueError, KeyError, AttributeError) as diagnostic_error:
                error.diagnostics_error_type = type(diagnostic_error).__name__
                LOGGER.error('Cannot preserve rejected plan (%s); original failure retained', type(diagnostic_error).__name__)
            raise
        self.tf(name, 'apply', '-input=false', '-no-color', 'stage.tfplan')
        after = self.identities(name)
        if destroy:
            if after:
                raise RuntimeError('Stage state is not empty')
            return {}
        if any(after.get(address) != identifier for address, identifier in before.items()):
            raise RuntimeError('Stage resource identity changed')
        return json.loads(self.tf(name, 'output', '-json', 'result'))
