#!/usr/bin/env python3
"""Boundary tests for the native executor.

OMEGA_RUN_BINARY selects the executor under test (default: the deployed binary).
Every test uses a private HOME, so evidence never touches the real tasks root.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = os.environ.get('OMEGA_RUN_BINARY', str(Path.home() / '.agents/tools/robot-exec/native/run'))


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        (self.home / '.agents/tasks').mkdir(parents=True)
        self.env = {**os.environ, 'HOME': str(self.home)}

    def run_binary(self, *args, **kwargs):
        return subprocess.run([BINARY, *args], capture_output=True, env=self.env, **kwargs)

    def test_signal_exit_races(self):
        for delay in range(20):
            evidence = self.home / '.agents/tasks/evidence'
            before = set(evidence.glob('*')) if evidence.exists() else set()
            process = subprocess.Popen([BINARY, 'run', '--', 'sleep', '0.05'], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=self.env)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                created = set(evidence.glob('*')) - before if evidence.exists() else set()
                if any('"pid"' in (path / 'result.json').read_text() for path in created if (path / 'result.json').exists()):
                    break
                time.sleep(0.001)
            time.sleep(delay * 0.004)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(stderr, b'')
            result = json.loads(stdout)
            self.assertIn(result['status'], ('PASS', 'INTERRUPTED'))
            self.assertEqual(process.returncode, 0 if result['status'] == 'PASS' else 143)
            metadata = json.loads((Path(result['evidence']) / 'result.json').read_text())
            self.assertEqual(metadata['status'], result['status'])
            self.assertEqual(metadata['exit_code'], process.returncode)

    def test_missing_tasks_root_fails_and_ci_initializes_it_before_first_run(self):
        fresh = self.home / 'fresh'
        fresh.mkdir()
        process = subprocess.run([BINARY, 'run', '--', 'true'], capture_output=True,
                                 env={**self.env, 'HOME': str(fresh)})
        self.assertEqual(process.returncode, 2)
        self.assertFalse((fresh / '.agents').exists())
        workflow = (Path(__file__).resolve().parents[3] / '.github/workflows/tests.yml').read_text()
        init = workflow.index('"$HOME/.agents/tasks"')
        self.assertLess(init, workflow.index('release/run run'))

    def test_symlink_evidence_root_is_rejected_without_changes(self):
        target = self.home / 'elsewhere'
        target.mkdir(mode=0o755)
        (self.home / '.agents/tasks/evidence').symlink_to(target)
        process = self.run_binary('run', '--', 'true')
        self.assertEqual(process.returncode, 2)
        self.assertIn('error', json.loads(process.stdout))
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_native_only_fails_closed(self):
        for options in (['--cwd', str(self.home / 'missing'), '--', 'true'], ['--unknown', 'x', '--', 'true'], ['--cwd']):
            process = self.run_binary('run', '--native-only', *options)
            self.assertEqual(process.returncode, 2, options)
            self.assertEqual(json.loads(process.stdout), {'status': 'FAIL', 'error': 'Unsupported input for native-only execution'})
        process = self.run_binary('run', '--native-only', '--', 'echo', 'café')
        self.assertEqual(json.loads(process.stdout)['error'], 'Unsupported input for native-only execution')
        self.assertEqual(list((self.home / '.agents/tasks').iterdir()), [])

    def test_unicode_invalid_utf8_and_ansi_output(self):
        code = "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\x1b[31mred\\x1b[0m \\xff ok\\n')"
        process = self.run_binary('run', '--', sys.executable, '-c', code)
        self.assertEqual(process.returncode, 0)
        result = json.loads(process.stdout)
        self.assertEqual(result['stdout'], 'café red � ok\n')
        self.assertEqual(result['bytes'], {'stdout': len(Path(result['evidence'], 'stdout.log').read_bytes()), 'stderr': 0})

    def test_invalid_cwd_is_a_command_failure(self):
        missing = str(self.home / 'missing')
        process = self.run_binary('run', '--cwd', missing, '--', 'true')
        self.assertEqual(process.returncode, 127)
        result = json.loads(process.stdout)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['stderr'], f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'\n")


if __name__ == '__main__':
    unittest.main()
