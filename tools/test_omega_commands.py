"""Short command dispatch and guard policy, using isolated HOME/PATH links."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get('OMEGA_RUN_BINARY', ROOT / 'rust/target/release/run')).resolve()
GUARD = Path(os.environ.get('OMEGA_SHELL_GUARD_BINARY', ROOT / 'rust/target/release/shell_guard')).resolve()


class OmegaCommandsTest(unittest.TestCase):
    prefix = 'omega-'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / '.agents/tasks').mkdir(parents=True)
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        spec = importlib.util.spec_from_file_location('omega_install', ROOT / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        installer.HOME = self.home
        installer.COMMANDS = {
            name: self.home / '.agents/tools' / target.relative_to(installer.AGENTS / 'tools')
            for name, target in installer.COMMANDS.items()
        }
        for target in set(installer.COMMANDS.values()):
            target.parent.mkdir(parents=True, exist_ok=True)
            binary = 'run' if target.name == 'run' else ('optimize' if target.name == 'optimize' else 'local_read')
            target.symlink_to(RUN.parent / binary)
        self.assertTrue(all(status == 'linked' for _, status in installer.install_commands(True)))
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{self.bin}:/usr/bin:/bin')

    def decision(self, *argv):
        argv = [arg.replace('omega-', self.prefix, 1) if i == 0 else arg
                for i, arg in enumerate(argv)]
        payload = {'tool_name': 'exec_command', 'tool_input': {'cmd': shlex.join(argv)}}
        result = subprocess.run([str(GUARD)], input=json.dumps(payload), text=True,
                                capture_output=True, env=self.env, check=True)
        return json.loads(result.stdout).get('hookSpecificOutput', {}).get('permissionDecision')

    def test_dispatch_run_inspect_and_prune(self):
        result = subprocess.run([self.prefix + 'run', '--', '/bin/echo', 'short command'],
                                env=self.env, capture_output=True, text=True, check=True)
        captured = json.loads(result.stdout)
        self.assertEqual(captured['stdout'], 'short command\n')
        result = subprocess.run([self.prefix + 'inspect', captured['evidence'], '--stream', 'stdout'],
                                env=self.env, capture_output=True, text=True, check=True)
        self.assertIn('short command', result.stdout)
        result = subprocess.run([self.prefix + 'prune'], env=self.env, capture_output=True, text=True, check=True)
        self.assertTrue(result.stdout)

    def test_policy_and_approval_are_preserved(self):
        for argv, expected in [
            (['omega-run', '--', 'git', 'status'], None),
            (['omega-run', '--shell', 'git status'], None),
            (['omega-run', '--', 'git', 'push', '--force'], 'deny'),
            (['omega-run', '--', 'ssh', 'robot', 'ls'], 'ask'),
            (['omega-inspect', '/tmp/evidence'], None),
            (['omega-prune'], None),
            (['omega-prune', '--apply'], 'ask'),
            (['omega-read', 'status'], None),
            (['omega-read', 'text', 'AGENTS.md'], None),
            (['omega-read', 'unknown'], 'deny'),
            (['omega-optimize', '--sessions', '2'], None),
            (['omega', 'optimize'], None),
            (['omega-resume'], None),
            (['omega-resume', 'task'], None),
            (['omega', 'resume'], None),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(self.decision(*argv), expected)

    def test_optimize_dispatch(self):
        for argv in ([self.prefix + 'optimize', '--json'], ['omega', 'optimize', '--json']):
            result = subprocess.run(argv, env=self.env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['sessions'], [])

    def test_resume_dispatch(self):
        task = self.home / '.agents/tasks/active/demo.md'
        task.parent.mkdir(parents=True)
        task.write_text('Objective: demo.\n')
        for argv in ([self.prefix + 'resume', 'demo'], ['omega', 'resume', 'demo']):
            result = subprocess.run(argv, env=self.env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['content'], 'Objective: demo.\n')

    def test_shadowed_resume_alias_is_denied(self):
        shadow = self.home / 'resume-shadow'
        shadow.mkdir()
        (shadow / (self.prefix + 'resume')).symlink_to('/bin/echo')
        self.env['PATH'] = f'{shadow}:{self.bin}:/usr/bin:/bin'
        self.assertEqual(self.decision('omega-resume'), 'deny')

    def test_shadowed_optimize_alias_is_denied(self):
        shadow = self.home / 'optimize-shadow'
        shadow.mkdir()
        (shadow / (self.prefix + 'optimize')).symlink_to('/bin/echo')
        self.env['PATH'] = f'{shadow}:{self.bin}:/usr/bin:/bin'
        self.assertEqual(self.decision('omega-optimize'), 'deny')

    def test_shadowed_and_missing_aliases_are_denied(self):
        shadow = self.home / 'shadow'
        shadow.mkdir()
        (shadow / (self.prefix + 'run')).symlink_to('/bin/echo')
        self.env['PATH'] = f'{shadow}:{self.bin}:/usr/bin:/bin'
        self.assertEqual(self.decision('omega-run', '--', 'true'), 'deny')
        self.env['PATH'] = '/usr/bin:/bin'
        self.assertEqual(self.decision('omega-run', '--', 'true'), 'deny')

    def test_outer_shell_execution_is_denied(self):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': 'omega-run -- true; echo leak'}}
        result = subprocess.run([str(GUARD)], input=json.dumps(payload), text=True,
                                capture_output=True, env=self.env, check=True)
        self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')


class OmegaSymbolCommandsTest(OmegaCommandsTest):
    prefix = 'Ω-'


if __name__ == '__main__':
    unittest.main()
