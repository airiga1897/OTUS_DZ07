"""Read-only YC discovery. Reuses CLI login, never records tokens or config."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def collect(binary, config):
    def yc(*args):
        command = [str(binary), '--no-browser', '--config', str(config),
                   '--format', 'json', *args]
        result = subprocess.run(command, capture_output=True, timeout=50,
                                stdin=subprocess.DEVNULL, encoding='utf-8')
        if result.returncode:
            # Authentication diagnostics can contain login URLs; keep raw output local.
            ROOT.joinpath('.local').mkdir(exist_ok=True)
            ROOT.joinpath('.local/yc-error.txt').write_text(result.stderr, encoding='utf-8')
            raise RuntimeError('YC command failed; inspect .local/yc-error.txt')
        return json.loads(result.stdout)

    report = {'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'read_only': True, 'clouds': []}
    clouds = yc('resource-manager', 'cloud', 'list')
    for cloud in clouds:
        entry = {'id': cloud['id'], 'name': cloud.get('name'),
                 'organization_id': cloud.get('organization_id')}
        entry['folders'] = yc('resource-manager', 'folder', 'list', '--cloud-id', cloud['id'])
        entry['quota_limits'] = {}
        for service in ('compute', 'vpc'):
            entry['quota_limits'][service] = yc('quota-manager', 'quota-limit', 'list',
                                               '--service', service,
                                               '--resource-type', 'resource-manager.cloud',
                                               '--resource-id', cloud['id'])
        report['clouds'].append(entry)
    output = ROOT / '.local/yc-preflight.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Read-only report saved: {output}; clouds: {len(clouds)}')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--yc', type=Path, default=ROOT.parent / 'OTUS_DZ04/.tools/yc.exe')
    parser.add_argument('--config', type=Path, default=ROOT.parent / 'OTUS_DZ04/.local/yc-config.yaml')
    options = parser.parse_args()
    collect(options.yc, options.config)
