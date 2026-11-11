import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallChecks(unittest.TestCase):
    def test_private_config_is_atomic_and_not_world_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'config.json'
            install.private_json(path, {'controller_secret': 'test-placeholder'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['controller_secret'], 'test-placeholder')
            self.assertFalse(path.with_suffix('.json.next').exists())

    def test_different_application_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)/'Other.app'
            (app/'Contents').mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(
                plistlib.dumps({'CFBundleIdentifier': 'example.unrelated'}))
            with self.assertRaises(RuntimeError):
                install.validate_app(app)

    def test_generated_launch_agents_use_installation_paths(self):
        runtime = Path('/tmp/example-meter')
        job = install.launch_agent(install.SAMPLER, ['/usr/bin/python3', runtime/'traffic_guard.py', 'run'],
                                  runtime, True)
        self.assertEqual(job['WorkingDirectory'], str(runtime))
        self.assertEqual(job['ProgramArguments'][1], str(runtime/'traffic_guard.py'))
        self.assertEqual(job['KeepAlive'], True)

    def test_stop_refuses_a_job_whose_pid_does_not_match_its_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime/'data').mkdir()
            (runtime/'data/daemon.pid.json').write_text(json.dumps({'pid': 456}))
            output = subprocess.CompletedProcess([], 0, stdout='pid = 123\n')
            with patch.object(install.subprocess, 'run', return_value=output) as run:
                with self.assertRaises(RuntimeError):
                    install.stop_job(install.SAMPLER, runtime, Path('/tmp/Example.app'))
                self.assertEqual(run.call_count, 1)

    def test_stop_verifies_identity_before_unloading_its_own_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime/'data').mkdir()
            (runtime/'data/daemon.pid.json').write_text(json.dumps({'pid': 123}))
            output = subprocess.CompletedProcess([], 0, stdout='pid = 123\n')
            identity = f'{os.getuid()} /usr/bin/python3 {runtime}/traffic_guard.py run\n'
            with patch.object(install.subprocess, 'run', return_value=output) as run, \
                    patch.object(install.subprocess, 'check_output', return_value=identity):
                install.stop_job(install.SAMPLER, runtime, Path('/tmp/Example.app'))
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][:2], ['launchctl', 'bootout'])


if __name__ == '__main__':
    unittest.main()
