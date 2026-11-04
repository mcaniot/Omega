"""Read guard contracts and non-destructive Claude hook installation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('OMEGA_CONTEXT_GUARD_BINARY', ROOT / 'rust/target/release/context_guard'))


class ContextGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def evaluate(self, path, **extra):
        result = subprocess.run([BINARY], input=json.dumps({
            'tool_name': 'Read', 'tool_input': {'file_path': str(path), **extra}}),
            text=True, capture_output=True, check=True,
            env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(result.stderr, '')
        value = json.loads(result.stdout)
        self.assertNotEqual(value.get('hookSpecificOutput', {}).get('permissionDecision'), 'allow')
        return value

    def large_file(self, name='source.py'):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('line\n' * 2000)
        return path

    def test_large_text_requires_small_explicit_range(self):
        path = self.large_file()
        for limit in (None, 0, -1, 201, True, '100', 1.5):
            with self.subTest(limit=limit):
                result = self.evaluate(path, **({} if limit is None else {'limit': limit}))
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        for limit in (1, 200):
            self.assertEqual(self.evaluate(path, limit=limit, offset=10), {})

    def test_evidence_requires_inspect_even_with_read_limit_or_symlink(self):
        path = self.large_file('.agents/tasks/evidence/run/stdout.log')
        alias = self.home / 'alias.log'
        alias.symlink_to(path)
        for file in (path, alias):
            result = self.evaluate(file, limit=20)
            self.assertIn('Ω-inspect', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_small_missing_and_media_files_keep_native_behavior(self):
        small = self.home / 'small.py'
        small.write_text('x' * 8192)
        self.assertEqual(self.evaluate(small), {})
        self.assertEqual(self.evaluate(self.home / 'missing'), {})
        for name in ('image.PNG', 'document.pdf', 'notebook.ipynb'):
            self.assertEqual(self.evaluate(self.large_file(name)), {})

    def test_other_tools_are_unchanged_and_invalid_json_is_denied(self):
        for payload, expected in ((b'{"tool_name":"Bash"}', {}), (b'{', None)):
            done = subprocess.run([BINARY], input=payload, capture_output=True, check=True)
            result = json.loads(done.stdout)
            if expected is None:
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
            else:
                self.assertEqual(result, expected)


class ContextSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('omega_context_install', ROOT / 'install.py')
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.installer.CLAUDE = self.home / '.claude'
        self.installer.CLAUDE.mkdir()
        self.settings = self.installer.CLAUDE / 'settings.json'
        self.command = str(self.installer.BINARIES['context_guard'])
        self.original = {'permissions': {'deny': ['keep']}, 'hooks': {
            'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': 'third-party'}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'keep'}]}]}}
        self.settings.write_text(json.dumps(self.original))
        self.settings.chmod(0o600)

    def test_dry_run_preservation_idempotence_and_symlink(self):
        self.assertEqual(len(list(self.installer.write_context_settings(False))), 1)
        self.assertEqual(json.loads(self.settings.read_text()), self.original)
        target = self.home / 'managed.json'
        self.settings.rename(target)
        self.settings.symlink_to(target)
        list(self.installer.write_context_settings(True))
        self.assertTrue(self.settings.is_symlink())
        actual = json.loads(target.read_text())
        added = actual['hooks']['PreToolUse'].pop()
        self.assertEqual(added, {'matcher': 'Read', 'hooks': [{'type': 'command', 'command': self.command}]})
        self.assertEqual(actual, self.original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.installer.write_context_settings(True)), [])

    def test_invalid_settings_do_not_write(self):
        self.settings.write_text('{invalid')
        with self.assertRaises(json.JSONDecodeError):
            list(self.installer.write_context_settings(True))
        self.assertEqual(self.settings.read_text(), '{invalid')

    def test_context_only_deploys_only_context_tools_and_aliases(self):
        with patch('sys.argv', ['install.py', '--context-only']), \
                patch.object(self.installer, 'install_binaries', return_value=[]) as binaries, \
                patch.object(self.installer, 'install_commands', return_value=[]) as commands, \
                patch.object(self.installer, 'write_response_settings') as responses:
            self.assertEqual(self.installer.main(), 0)
        binaries.assert_called_once_with(False, names=('context_guard', 'optimize', 'shell_guard'))
        commands.assert_called_once_with(False, names=('omega', 'omega-optimize', 'Ω-optimize'))
        responses.assert_not_called()


if __name__ == '__main__':
    unittest.main()
