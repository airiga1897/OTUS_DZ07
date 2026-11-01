"""Check the real OS lock across processes and after nested failures."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stage_store import operation

ROOT=Path(__file__).resolve().parents[1]


class ProcessLockContracts(unittest.TestCase):
    def test_nested_failure_does_not_release_outer_lock_and_exit_releases_it(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as folder:
            local=Path(folder)/'.local'
            code='from pathlib import Path\nfrom stage_store import operation\nimport sys\nwith operation(Path(sys.argv[1])): print("acquired")'
            env={**os.environ,'PYTHONPATH':str(ROOT/'scripts')}
            def child():
                return subprocess.run([sys.executable,'-c',code,str(local)],env=env,capture_output=True,timeout=20)
            with operation(local):
                with self.assertRaisesRegex(RuntimeError,'nested'):
                    with operation(local):raise RuntimeError('nested')
                self.assertNotEqual(child().returncode,0)
            result=child()
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            self.assertEqual(result.stdout.strip(),b'acquired')
