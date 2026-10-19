"""Из ДЗ04: завершённый cloud-init с предупреждениями не является аварией."""
import json
import subprocess


def completed(result, code):
    stages = [result] + [v for v in result.values() if isinstance(v, dict)]
    return code in (0, 2) and result.get('status') == 'done' and all(not s.get('errors') for s in stages)


if __name__ == '__main__':
    run = subprocess.run(['cloud-init', 'status', '--wait', '--format', 'json'], capture_output=True, text=True)
    result = json.loads(run.stdout)
    if not completed(result, run.returncode):
        raise SystemExit('cloud-init incomplete or fatal errors; inspect status --long.')
    print('cloud-init completed; recoverable warnings present:', bool(result.get('recoverable_errors')))
