"""DZ07-only systemd Ansible operation protocol. No credentials in RPC output."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.next')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(path: Path, nonblocking: bool = False) -> Iterator[None]:
    import fcntl
    with path.open('a+b') as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        try: yield
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


class Manager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.home = self.root / '.ansible-operations'
        if self.home.is_symlink(): raise ValueError('Operation directory link forbidden')
        self.home.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.home, 0o700)

    def folder(self, identity: str) -> Path:
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
            raise ValueError('Invalid operation identity')
        path = self.home / identity
        if path.is_symlink(): raise ValueError('Operation link forbidden')
        return path

    def unit_name(self, request: dict) -> str:
        return request['namespace'] + '-ansible-' + request['id'] + '.service'

    def validate(self, request: dict) -> None:
        self.folder(request['id'])
        if (not re.fullmatch('[a-f0-9]{32}', request['deployment_id'])
                or not re.fullmatch('[a-z][a-z0-9-]+', request['namespace'])
                or not re.fullmatch('[A-Za-z0-9-]+', request['app_id'])
                or not re.fullmatch('[a-f0-9]{64}', request['parameters_sha256'])
                or type(request['timeout']) is not int or not 1 <= request['timeout'] <= 86400
                or type(request['stop_wait']) is not int or not 1 <= request['stop_wait'] <= 120):
            raise ValueError('Invalid operation binding')
        if (set(request.get('payload', {})) != {'inventory.json','controls.json','key','known_hosts'}
                or any(not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha) for sha in request['payload'].values())):
            raise ValueError('Invalid private payload binding')

    def boot(self) -> str:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def saved(self, request: dict) -> dict:
        saved = json.loads((self.folder(request['id']) / 'request.json').read_text())
        if saved['request'] != request: raise ValueError('Operation parameters or ownership differ')
        return saved

    def unit(self, request: dict) -> dict:
        value = subprocess.run(['systemctl', 'show', self.unit_name(request), '--no-pager',
            '--property=LoadState,ActiveState,ControlGroup,ExecMainStatus,Result'],
            capture_output=True, text=True, timeout=15)
        properties = dict(line.split('=', 1) for line in value.stdout.splitlines() if '=' in line)
        if value.returncode and properties.get('LoadState') != 'not-found':
            raise RuntimeError('Cannot inspect operation service')
        if properties.get('LoadState') not in ('loaded', 'not-found'):
            raise RuntimeError('Unknown operation service state')
        active = properties.get('ActiveState') not in ('inactive', 'failed')
        group = properties.get('ControlGroup')
        if group:
            base = Path('/sys/fs/cgroup').resolve()
            path = (base / group.lstrip('/')).resolve()
            if not path.is_relative_to(base): raise ValueError('Invalid service control group')
            active |= any(file.read_text().strip() for file in path.rglob('cgroup.procs'))
        return {'active': active, 'loaded': properties['LoadState'] == 'loaded'}

    def status(self, request: dict) -> dict:
        saved = self.saved(request)
        folder = self.folder(request['id'])
        unit = self.unit(request)
        try:
            result = json.loads((folder / 'result.json').read_text())
            if result.get('id') != request['id'] or result.get('parameters_sha256') != request['parameters_sha256']:
                raise ValueError('Invalid result binding')
            if result.get('state') not in ('preparing', 'running', 'succeeded', 'failed', 'stopped'):
                raise ValueError('Invalid result state')
        except (OSError, ValueError):
            return {'state': 'running' if unit['active'] else 'unknown', 'reason': 'result_missing_or_invalid'}
        if unit['active']:
            return {**result, 'state': 'running'}
        if result['state'] == 'running' or (result['state'] == 'preparing' and (saved['boot_id'] != self.boot() or unit['loaded'])):
            return {**result, 'state': 'unknown', 'reason': 'service_ended_without_result'}
        if result['state'] in ('succeeded', 'failed') and (type(result.get('exit_code')) is not int
                or not result.get('started_utc') or not result.get('finished_utc')
                or (result['state'] == 'succeeded' and result['exit_code'] != 0)):
            return {**result, 'state': 'unknown', 'reason': 'incomplete_result'}
        return result

    def reserve(self, request: dict) -> dict:
        self.validate(request)
        with locked(self.home / 'lock'):
            binding = {k: request[k] for k in ('deployment_id', 'app_id', 'namespace')}
            path = self.home / 'binding.json'
            if path.exists() and json.loads(path.read_text()) != binding:
                raise ValueError('Application VM belongs to another deployment')
            active = self.home / 'active.json'
            if active.exists() and json.loads(active.read_text())['id'] != request['id']:
                raise ValueError('Another configuration operation must be acknowledged or stopped')
            folder = self.folder(request['id'])
            if folder.exists():
                self.saved(request)
            else:
                folder.mkdir(mode=0o700)
                atomic(folder / 'request.json', {'request': request, 'boot_id': self.boot()})
                atomic(folder / 'result.json', {'id': request['id'], 'parameters_sha256': request['parameters_sha256'],
                    'state': 'preparing', 'created_utc': now(), 'started_utc': None, 'finished_utc': None, 'exit_code': None})
            atomic(path, binding)
            atomic(active, {'id': request['id']})
            return self.status(request)

    def start(self, request: dict) -> dict:
        with locked(self.home / 'lock'):
            self.saved(request)
            result = self.status(request)
            if result['state'] != 'preparing': return result
            folder = self.folder(request['id'])
            if not (folder / 'source').exists():
                with tempfile.TemporaryDirectory(dir=folder) as temp:
                    snapshot = Path(temp) / 'source'; snapshot.mkdir()
                    for name in ('ansible', 'scripts', 'config'):
                        shutil.copytree(self.root / name, snapshot / name, ignore=shutil.ignore_patterns('__pycache__'))
                    snapshot.rename(folder / 'source')
            for name, sha in request['payload'].items():
                if name not in ('inventory.json', 'controls.json', 'key', 'known_hosts'):
                    raise ValueError('Unexpected private operation payload')
                with (folder / name).open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                        raise ValueError('Incomplete operation payload')
                os.chmod(folder / name, 0o600)
            shutil.copyfile(__file__, folder / 'runner.py')
            os.chmod(folder / 'runner.py', 0o600)
            # The unit name is stable: a lost RPC response cannot create a second job.
            if not self.unit(request)['loaded']:
                subprocess.run(['sudo', '-n', 'systemd-run', '--quiet', '--unit=' + self.unit_name(request),
                    '--uid=otus', '--property=UMask=0077', '--property=KillMode=control-group',
                    '--property=TimeoutStopSec=' + str(request['stop_wait']), '--property=SendSIGKILL=yes',
                    '--property=RuntimeMaxSec=' + str(request['timeout'] + request['stop_wait'] + 10),
                    '--property=Restart=no', '/usr/bin/python3', str(folder / 'runner.py'),
                    '--root', str(self.root), 'run', request['id']], check=True, capture_output=True, timeout=20)
            return self.status(request)

    def stop(self, request: dict) -> dict:
        with locked(self.home / 'lock'):
            self.saved(request)
            if self.unit(request)['loaded']:
                subprocess.run(['sudo', '-n', 'systemctl', 'stop', self.unit_name(request)],
                    check=True, capture_output=True, timeout=request['stop_wait'] + 15)
            if self.unit(request)['active']: raise RuntimeError('Operation or child processes still active')
            previous = self.status(request)
            result = {'id': request['id'], 'parameters_sha256': request['parameters_sha256'],
                'state': 'stopped', 'finished_utc': now(), 'exit_code': 143}
            if previous['state'] in ('succeeded', 'failed', 'stopped'):
                # Stop confirmation is separate from the Ansible outcome.
                # A finished playbook's code and timestamps must not be lost.
                return {**result, 'operation_result': previous}
            atomic(self.folder(request['id']) / 'result.json', result)
            return result

    def acknowledge(self, request: dict) -> dict:
        with locked(self.home / 'lock'):
            result = self.status(request)
            if result['state'] not in ('succeeded', 'failed', 'stopped') or self.unit(request)['active']:
                raise ValueError('Operation completion is unconfirmed')
            path = self.home / 'active.json'
            if path.exists():
                if json.loads(path.read_text())['id'] != request['id']:
                    raise ValueError('Another operation owns configuration')
                path.unlink()
            return result


def execute(manager: Manager, identity: str) -> None:
    folder = manager.folder(identity)
    request = json.loads((folder / 'request.json').read_text())['request']
    with locked(folder / 'runner.lock', nonblocking=True):
        result = manager.status(request)
        # systemd may be activating; only a previously persisted start prevents re-entry.
        if result.get('started_utc') or result.get('state') == 'stopped': return
        result.update(id=identity, parameters_sha256=request['parameters_sha256'], state='running', started_utc=now())
        atomic(folder / 'result.json', result)
        child = None
        def stopped(signum, frame): raise InterruptedError('Operation stopped')
        old_handler = signal.signal(signal.SIGTERM, stopped)
        deadline = time.monotonic() + request['timeout']
        phase, code, reason = 'prepare', 1, None
        try:
            inventory = json.loads((folder / 'inventory.json').read_text())
            inventory['all']['vars'].update(ansible_ssh_private_key_file=str(folder / 'key'),
                ansible_ssh_common_args='-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o UserKnownHostsFile=' + str(folder / 'known_hosts'))
            atomic(folder / 'inventory.json', inventory)
            source = folder / 'source'
            stack = json.loads((source / 'config/stack.json').read_text())
            command = [stack['ansible']['venv'] + '/bin/ansible-playbook', '--extra-vars', '@' + str(folder / 'controls.json'),
                       '-i', str(folder / 'inventory.json'), 'site.yml']
            steps = [('prepare', ['sudo', '-n', '/usr/bin/python3', str(source / 'scripts/remote/ansible_control.py')]),
                     ('syntax', command + ['--syntax-check']), ('configure', command)]
            environment = dict(os.environ, ANSIBLE_CONFIG=str(source / 'ansible/ansible.cfg'))
            with (folder / 'operation.log').open('a') as log:
                os.chmod(folder / 'operation.log', 0o600)
                for phase, args in steps:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise subprocess.TimeoutExpired(args, request['timeout'])
                    child = subprocess.Popen(args, cwd=source / 'ansible', env=environment,
                        stdout=log, stderr=log, start_new_session=True)
                    code = child.wait(timeout=remaining)
                    if code: break
        except subprocess.TimeoutExpired:
            code, reason = 124, 'operation_timeout'
        except InterruptedError:
            code, reason = 143, 'stopped'
        except Exception:
            code, reason = 1, 'worker_error'
        finally:
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=request['stop_wait'])
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait()
                except (ProcessLookupError, PermissionError):
                    # systemd owns the entire control group, including sudo
                    # descendants. It kills residual children on service exit;
                    # status() stays running until the group is empty.
                    pass
            signal.signal(signal.SIGTERM, old_handler)
            result.update(state='succeeded' if code == 0 else 'stopped' if code == 143 else 'failed',
                phase=phase, exit_code=code, reason=reason, finished_utc=now())
            atomic(folder / 'result.json', result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('action', choices=['rpc', 'run'])
    parser.add_argument('id', nargs='?')
    args = parser.parse_args()
    manager = Manager(args.root)
    if args.action == 'run':
        execute(manager, args.id)
    else:
        import sys
        data = json.load(sys.stdin)
        action, request = data['action'], data['request']
        manager.validate(request)
        if action not in ('reserve', 'start', 'status', 'stop', 'acknowledge'):
            raise ValueError('Unknown operation action')
        print(json.dumps(getattr(manager, action)(request)))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        raise SystemExit('Ansible operation protocol failed: ' + type(error).__name__) from None
