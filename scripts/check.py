"""Run isolated local checks with external networking disabled and honest branch coverage."""
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

import coverage

ROOT = Path(__file__).resolve().parents[1]


def offline_connection(*args, **kwargs):
    raise AssertionError('External networking is forbidden in local checks; mock the transport')


def main():
    os.chdir(ROOT)
    for key in list(os.environ):
        if key.startswith('OS_') or key == 'OTUS_SELECTEL_AUTH':
            os.environ.pop(key)
    os.environ.update(DJANGO_SECRET_KEY='local-tests-only', DJANGO_SETTINGS_MODULE='config.test_settings')
    sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'app')]
    output = ROOT / '.tools/coverage'
    output.mkdir(parents=True, exist_ok=True)
    omitted = [str(p) for p in (ROOT / 'scripts').glob('test_*.py') if p.name != 'test_cycle.py']
    omitted += [str(ROOT / 'tests/*'), str(ROOT / 'app/knowledge/test*.py'),
                str(ROOT / 'app/knowledge/migrations/*'), str(Path(__file__).resolve())]
    measurement = coverage.Coverage(branch=True, source=[str(ROOT / 'scripts'), str(ROOT / 'app')],
                                   omit=omitted, data_file=str(output / 'latest.data'))
    # Remote workers are namespace modules; unimported files must count as zero,
    # rather than silently disappearing from the coverage denominator.
    measurement.set_option('report:include_namespace_packages', True)
    measurement.start()
    results = {}
    try:
        with patch.object(socket.socket, 'connect', offline_connection), patch.object(socket, 'create_connection', offline_connection):
            for group in ('scripts', 'tests'):
                measurement.switch_context(group)
                suite = unittest.TestLoader().discover(str(ROOT / group), pattern='test_*.py')
                result = unittest.TextTestRunner(verbosity=1).run(suite)
                results[group] = {'tests': result.testsRun, 'success': result.wasSuccessful()}
            measurement.switch_context('django')
            import django
            django.setup()
            from django.test.runner import DiscoverRunner
            results['django'] = {'success': DiscoverRunner(verbosity=1).run_tests(['knowledge']) == 0}
    finally:
        measurement.stop()
        measurement.save()
        measurement.json_report(outfile=str(output / 'latest.json'))
        with (output / 'latest.txt').open('w') as stream:
            measurement.report(file=stream, show_missing=True)
        (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    totals = json.loads((output / 'latest.json').read_text())['totals']
    print(json.dumps({'results': results, 'coverage': totals}, indent=2))
    return 0 if results and all(item['success'] for item in results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
