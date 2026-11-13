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
        hook = value.get('hookSpecificOutput', {})
        # The guard allows only its own bounded rewrite, never the call as written.
        self.assertEqual(hook.get('permissionDecision') == 'allow', 'updatedInput' in hook)
        return value

    def large_file(self, name='source.py'):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('line\n' * 2000)
        return path

    def test_large_text_without_small_range_is_bounded_not_denied(self):
        path = self.large_file()
        for limit in (None, 0, -1, 201, True, '100', 1.5):
            with self.subTest(limit=limit):
                hook = self.evaluate(path, pages='keep', **({} if limit is None else {'limit': limit}))['hookSpecificOutput']
                self.assertEqual(hook['permissionDecision'], 'allow')
                self.assertEqual(hook['updatedInput'], {'file_path': str(path), 'pages': 'keep', 'limit': 200})
                self.assertIn(f'lines 1-200 of 2000 of {path}', hook['additionalContext'])
        hook = self.evaluate(path, offset=1901, limit=500)['hookSpecificOutput']
        self.assertEqual(hook['updatedInput'], {'file_path': str(path), 'offset': 1901, 'limit': 200})
        self.assertIn('lines 1901-2000 of 2000', hook['additionalContext'])
        for limit in (1, 200):
            self.assertEqual(self.evaluate(path, limit=limit, offset=10), {})

    def test_evidence_requires_inspect_even_with_read_limit_or_symlink(self):
        path = self.large_file('.agents/tasks/evidence/run/stdout.log')
        alias = self.home / 'alias.log'
        alias.symlink_to(path)
        for file in (path, alias):
            result = self.evaluate(file, limit=20)
            self.assertIn("'Ω-inspect' EVIDENCE_DIR", result['hookSpecificOutput']['permissionDecisionReason'])

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


class ReadLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'src.py'
        self.path.write_text(''.join(f'line {i}\n' for i in range(1, 1501)))  # > 8 KiB

    def run_hook(self, event='PreToolUse', raw=None, **payload):
        payload = {'hook_event_name': event, 'session_id': 's1', **payload}
        done = subprocess.run([BINARY], input=raw if raw is not None else json.dumps(payload), text=True,
                              capture_output=True, check=True, env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(done.stderr, '')
        return json.loads(done.stdout)

    def read(self, offset=None, limit=None, **extra):
        tool_input = {'file_path': str(self.path)}
        if offset is not None:
            tool_input['offset'] = offset
        if limit is not None:
            tool_input['limit'] = limit
        return self.run_hook(tool_name='Read', tool_input=tool_input, **extra)

    def reason(self, result):
        output = result['hookSpecificOutput']
        self.assertEqual(output['permissionDecision'], 'deny')
        return output['permissionDecisionReason']

    @property
    def ledger(self):
        return self.home / '.agents/state/read-ledger/s1.json'

    def test_full_cover_denies_and_ledger_is_private(self):
        self.assertEqual(self.read(1, 100), {})
        self.assertEqual(self.reason(self.read(10, 50)),
                         f'already in context: lines 10-59 of {self.path} (unchanged since read)')
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ledger.parent.stat().st_mode & 0o777, 0o700)

    def test_denied_read_is_not_recorded(self):
        self.assertEqual(self.read(1, 100), {})
        self.assertIn('offset=101 limit=50', self.reason(self.read(51, 100)))
        self.assertEqual(self.read(101, 150), {})  # still unread: the denial recorded nothing

    def test_partial_cover_suggests_first_gap(self):
        self.assertEqual(self.read(50, 51), {})
        self.assertIn('offset=1 limit=49', self.reason(self.read(1, 150)))
        self.assertIn('offset=101 limit=50', self.reason(self.read(50, 101)))

    def test_bounded_read_is_recorded_and_checked_as_its_real_range(self):
        hook = self.read()['hookSpecificOutput']  # 1500 lines, no limit: runs as lines 1-200
        self.assertEqual(hook['updatedInput']['limit'], 200)
        self.assertEqual(self.reason(self.read()), f'already in context: lines 1-200 of {self.path} (unchanged since read)')
        self.assertIn('offset=201 limit=149', self.reason(self.read(150)))
        self.assertEqual(self.read(201, 500)['hookSpecificOutput']['updatedInput']['offset'], 201)

    def test_changed_file_drops_ranges(self):
        self.assertEqual(self.read(1, 100), {})
        self.path.write_text(self.path.read_text() + 'more\n')
        self.assertEqual(self.read(1, 100), {})

    def test_first_cpp_edit_hints_cpp_quality_once_per_session_or_subagent(self):
        def edit(name, **extra):
            return self.run_hook('PostToolUse', tool_name='Edit', tool_input={'file_path': f'/w/{name}'}, **extra)
        self.assertEqual(edit('a.py'), {})
        hint = edit('ctrl/loop.CPP')['hookSpecificOutput']
        self.assertEqual(hint['hookEventName'], 'PostToolUse')
        self.assertIn('`cpp-quality`', hint['additionalContext'])
        self.assertEqual(edit('b.hpp'), {})
        self.assertIn('additionalContext', edit('b.hpp', agent_id='a1')['hookSpecificOutput'])
        self.assertEqual(edit('c.h', agent_id='a1'), {})
        self.assertIn('additionalContext', edit('c.h', session_id='s2')['hookSpecificOutput'])
        self.assertEqual(edit('c.h', session_id='../x'), {})
        self.assertEqual(self.run_hook('PreToolUse', tool_name='Edit', tool_input={'file_path': '/w/z.cpp'}), {})

    def test_edit_and_write_drop_ranges(self):
        for tool in ('Edit', 'Write', 'MultiEdit'):
            self.assertEqual(self.read(1, 100), {})
            before = self.path.stat()
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))  # same stamp: only the hook can drop
            self.assertEqual(self.run_hook('PostToolUse', tool_name=tool,
                                           tool_input={'file_path': str(self.path)}), {})
            self.assertEqual(self.read(1, 100), {})
            self.ledger.unlink()

    def test_compact_clear_and_post_compact_reset(self):
        for event, extra in (('SessionStart', {'source': 'compact'}), ('SessionStart', {'source': 'clear'}),
                             ('PostCompact', {})):
            self.assertEqual(self.read(1, 100), {})
            self.assertTrue(self.ledger.exists())
            self.assertEqual(self.run_hook(event, **extra), {})
            self.assertFalse(self.ledger.exists())
        self.assertEqual(self.read(1, 100), {})
        self.run_hook('SessionStart', source='startup')
        self.assertTrue(self.ledger.exists())

    def test_small_file_whole_read_and_subagent_and_sessions(self):
        self.path.write_text('a\nb\nc\n')
        self.assertEqual(self.read(), {})
        self.assertIn('lines 1-3 of', self.reason(self.read()))
        self.assertEqual(self.read(agent_id='a1'), {})  # subagent context is separate
        self.assertEqual(self.read(session_id='other'), {})

    def test_corrupt_or_unsafe_ledger_fails_open(self):
        self.ledger.parent.mkdir(parents=True)
        for garbage in (b'\xff{not json', b'[1,2]', b'x' * (2 << 20)):
            self.ledger.write_bytes(garbage)
            self.assertEqual(self.read(1, 100), {})
            self.assertIn('already in context', self.reason(self.read(1, 100)))
        self.assertEqual(self.read(1, 100, session_id='../escape'), {})
        self.assertEqual(self.run_hook('SessionStart', raw='{', source='clear')['hookSpecificOutput']
                         ['permissionDecision'], 'deny')  # unchanged garbage-input contract


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
        hooks = actual['hooks']
        guard = [{'matcher': 'Read', 'hooks': [{'type': 'command', 'command': self.command}]}]
        self.assertEqual(hooks['PreToolUse'][1], guard[0])
        self.assertEqual(hooks['PostToolUse'][0]['matcher'], 'Edit|Write|MultiEdit')
        budget = str(self.installer.BINARIES['context_budget'])
        retired = {'matcher': '*', 'hooks': [{'type': 'command', 'command': budget}]}
        self.assertEqual(hooks['PostToolUse'][-1], retired)
        self.assertNotIn(retired, hooks['PreToolUse'])
        for event in ('SessionStart', 'PostCompact', 'PreCompact', 'UserPromptSubmit'):
            self.assertIn({'hooks': [{'type': 'command', 'command': budget}]}, hooks[event])
        for event in ('SessionStart', 'PostCompact'):
            self.assertEqual(hooks[event][0], {'hooks': [{'type': 'command', 'command': self.command}]})
        for key, value in self.installer.CONTEXT_ENV.items():
            self.assertEqual(actual['env'][key], value)
        self.assertEqual(actual['permissions'], self.original['permissions'])
        self.assertEqual(hooks['Stop'], self.original['hooks']['Stop'])
        self.assertEqual(hooks['PreToolUse'][0], self.original['hooks']['PreToolUse'][0])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.installer.write_context_settings(True)), [])
        # Settings written by earlier versions: the tool gate and hard limit are removed, nothing else.
        actual['hooks']['PreToolUse'] = [*hooks['PreToolUse'], retired]
        actual['env']['OMEGA_CONTEXT_HARD'] = '130000'
        target.write_text(json.dumps(actual))
        self.assertEqual(len(list(self.installer.write_context_settings(True))), 1)
        cleaned = json.loads(target.read_text())
        shell = {'matcher': 'Bash', 'hooks': [{
            'type': 'command', 'command': f'{self.installer.BINARIES["shell_guard"]} --allow-clean',
            'timeout': 5, 'onFailure': 'block'}]}
        self.assertEqual(cleaned['hooks']['PreToolUse'], [self.original['hooks']['PreToolUse'][0], guard[0], shell])
        self.assertNotIn('OMEGA_CONTEXT_HARD', cleaned['env'])
        self.assertEqual(list(self.installer.write_context_settings(True)), [])

    def test_hand_registered_omega_hooks_are_normalised_in_place(self):
        binaries = self.installer.BINARIES
        naming = {'type': 'command', 'command': '"$HOME/.claude/hooks/session_name"'}
        self.settings.write_text(json.dumps({'hooks': {
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': str(binaries['shell_guard'])}]},
                           self.original['hooks']['PreToolUse'][0]],
            'SessionStart': [{'hooks': [naming]}]}}))
        list(self.installer.write_context_settings(True))
        hooks = json.loads(self.settings.read_text())['hooks']
        self.assertEqual(hooks['PreToolUse'][0]['hooks'], [{
            'type': 'command', 'command': f'{binaries["shell_guard"]} --allow-clean', 'timeout': 5, 'onFailure': 'block'}])
        self.assertEqual(hooks['PreToolUse'][1], self.original['hooks']['PreToolUse'][0])
        self.assertEqual(hooks['SessionStart'][0]['hooks'],
                         [{'type': 'command', 'command': str(binaries['session_name']), 'timeout': 5}])
        self.assertEqual(sum(str(binaries['shell_guard']) in json.dumps(group) for group in hooks['PreToolUse']), 1)
        self.assertEqual(list(self.installer.write_context_settings(True)), [])

    def test_codex_hook_is_added_once_and_existing_groups_keep_their_position(self):
        self.installer.CODEX = self.home / '.codex'
        self.installer.CODEX.mkdir()
        dest = self.installer.CODEX / 'hooks.json'
        other = {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'third-party'}]}
        dest.write_text(json.dumps({'hooks': {'PreToolUse': [other]}}))
        self.assertEqual(len(list(self.installer.write_codex_hooks(False))), 1)
        self.assertEqual(json.loads(dest.read_text()), {'hooks': {'PreToolUse': [other]}})
        list(self.installer.write_codex_hooks(True))
        ours = {'matcher': 'Bash', 'hooks': [{
            'type': 'command', 'command': str(self.installer.BINARIES['shell_guard']), 'timeout': 5}]}
        self.assertEqual(json.loads(dest.read_text())['hooks']['PreToolUse'], [other, ours])
        self.assertEqual(list(self.installer.write_codex_hooks(True)), [])

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
        binaries.assert_called_once_with(False, names=('context_guard', 'context_budget', 'optimize', 'shell_guard', 'session_name'))
        commands.assert_called_once_with(False, names=('omega', 'omega-optimize', 'Ω-optimize', 'omega-resume', 'Ω-resume'))
        responses.assert_not_called()


if __name__ == '__main__':
    unittest.main()
