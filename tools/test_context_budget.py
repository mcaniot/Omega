"""Context budget hook contracts: one checkpoint reminder per crossing, never a denial."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('OMEGA_CONTEXT_BUDGET_BINARY', ROOT / 'rust/target/release/context_budget'))


class ContextBudgetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / '.agents/tasks/active').mkdir(parents=True)
        self.transcript = self.home / 'transcript.jsonl'
        self.set_tokens(1000)

    def set_tokens(self, total, sidechain_tokens=None):
        lines = [{'type': 'user', 'message': {'content': 'hi'}},
                 {'type': 'assistant', 'message': {'usage': {
                     'input_tokens': 10, 'cache_creation_input_tokens': 20,
                     'cache_read_input_tokens': total - 30, 'output_tokens': 5}}}]
        if sidechain_tokens:
            lines.append({'type': 'assistant', 'isSidechain': True,
                          'message': {'usage': {'input_tokens': sidechain_tokens}}})
        self.transcript.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')

    def run_hook(self, event, raw=None, env=None, **payload):
        return json.loads(self.run_raw(event, raw, env, **payload))

    def run_raw(self, event, raw=None, env=None, **payload):
        """Stdout as printed: PreCompact emits plain-text compaction instructions or nothing."""
        payload = {'hook_event_name': event, 'session_id': 's1', 'transcript_path': str(self.transcript), **payload}
        done = subprocess.run([BINARY], input=raw if raw is not None else json.dumps(payload), text=True,
                              capture_output=True, check=True,
                              env={**{k: v for k, v in os.environ.items() if not k.startswith('OMEGA_CONTEXT_')},
                                   'HOME': str(self.home), **(env or {})})
        self.assertEqual(done.stderr, '')
        return done.stdout

    def test_soft_fires_once_per_crossing_and_rearms(self):
        self.assertEqual(self.run_hook('PostToolUse'), {})
        self.set_tokens(155_000)
        first = self.run_hook('PostToolUse')['hookSpecificOutput']
        self.assertEqual(first['hookEventName'], 'PostToolUse')
        self.assertEqual(first['additionalContext'],
                         'Context 155k: update ~/.agents/tasks/active/<task>.md now '
                         '(verified facts, state, next action), then keep working.')
        self.assertEqual(self.run_hook('PostToolUse'), {})
        self.assertEqual(self.run_hook('UserPromptSubmit'), {})
        self.set_tokens(1000)
        self.assertEqual(self.run_hook('UserPromptSubmit'), {})
        self.set_tokens(160_000)
        self.assertEqual(self.run_hook('UserPromptSubmit')['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')

    def test_reset_events_rearm_soft(self):
        self.set_tokens(155_000)
        self.run_hook('PostToolUse')
        note = self.run_hook('SessionStart', source='compact')['hookSpecificOutput']
        self.assertEqual(note['hookEventName'], 'SessionStart')
        self.assertIn('omega resume', note['additionalContext'])
        self.assertEqual(self.run_hook('SessionStart', source='clear'), {})
        self.assertEqual(self.run_hook('SessionStart', source='startup'), {})
        self.assertIn('additionalContext', self.run_hook('PostToolUse')['hookSpecificOutput'])
        self.assertEqual(self.run_hook('PostCompact'), {})
        self.assertIn('additionalContext', self.run_hook('PostToolUse')['hookSpecificOutput'])

    def test_usage_older_than_the_compact_summary_is_ignored(self):
        self.set_tokens(216_000)
        summary = {'type': 'user', 'isCompactSummary': True, 'message': {'content': 'summary'}}
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(summary, separators=(',', ':')) + '\n')
        self.assertEqual(self.run_hook('PostToolUse'), {})
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 151_000}}}) + '\n')
        self.assertIn('Context 151k', self.run_hook('PostToolUse')['hookSpecificOutput']['additionalContext'])

    def test_env_threshold_and_sidechain_ignored(self):
        self.set_tokens(5000, sidechain_tokens=999_999)
        env = {'OMEGA_CONTEXT_SOFT': '4000'}
        self.assertIn('Context 5k:', self.run_hook('PostToolUse', env=env)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.run_hook('PostToolUse', env={'OMEGA_CONTEXT_SOFT': '6000'}, session_id='s2'), {})

    def test_never_denies_a_tool_whatever_the_context(self):
        self.set_tokens(990_000)
        tasks = self.home / '.agents/tasks'
        for env in ({}, {'OMEGA_CONTEXT_SOFT': '100000', 'OMEGA_CONTEXT_HARD': '130000'}):
            for tool, tool_input in (
                    ('Bash', {'command': 'ls'}), ('Read', {'file_path': str(self.home / 'x.py')}),
                    ('Edit', {'file_path': str(self.home / 'x.py')}), ('Grep', {'pattern': 'x'}),
                    ('Write', {'file_path': str(tasks / 'active/new.md')})):
                with self.subTest(tool=tool, env=env):
                    self.assertEqual(self.run_hook('PreToolUse', env=env, tool_name=tool, tool_input=tool_input), {})
        self.assertEqual(self.run_hook('PostToolUse', agent_id='a1'), {})  # subagents get no reminder

    def test_observed_auto_compaction_caps_the_reminder(self):
        env = {'OMEGA_CONTEXT_SOFT': '850000'}
        floor = self.home / '.agents/state/budget/compact-floor.json'
        self.set_tokens(217_000)
        self.assertEqual(self.run_hook('PostToolUse', env=env), {})
        for ignored in ({'trigger': 'manual'}, {'trigger': 'auto', 'agent_id': 'a1'}):
            self.run_raw('PreCompact', env=env, **ignored)
            self.assertFalse(floor.exists())
        self.run_raw('PreCompact', env=env, trigger='auto')
        self.assertEqual(json.loads(floor.read_text()), {'tokens': 217_000})
        self.set_tokens(149_000)  # 69 % of 217k = 149.7k
        self.assertEqual(self.run_hook('PostToolUse', env=env), {})
        self.set_tokens(150_000)
        self.assertIn('Context 150k:', self.run_hook('PostToolUse', env=env)['hookSpecificOutput']['additionalContext'])
        os.utime(floor, (0, 0))  # stale observation: the configured limit applies again
        self.assertEqual(self.run_hook('PostToolUse', env=env, session_id='s2'), {})
        floor.write_text('{"tokens": 10}')  # implausible floor is ignored
        self.assertEqual(self.run_hook('PostToolUse', env=env, session_id='s3'), {})
        self.set_tokens(10_000)  # implausible compaction point is not recorded
        self.run_raw('PreCompact', trigger='auto')
        self.assertEqual(json.loads(floor.read_text()), {'tokens': 10})

    def test_precompact_stdout_is_the_summary_instruction(self):
        for payload in ({'trigger': 'auto'}, {'trigger': 'manual', 'custom_instructions': None},
                        {'trigger': 'manual', 'custom_instructions': ' '}):
            text = self.run_raw('PreCompact', **payload)
            self.assertIn('~/.agents/tasks/active/', text)
            self.assertLess(len(text), 10_000)  # documented cap for PreCompact stdout
            self.assertFalse(text.lstrip().startswith('{'))
        # The user's own /compact instructions and subagent compactions get none.
        self.assertEqual(self.run_raw('PreCompact', trigger='manual', custom_instructions='keep the API notes'), '')
        self.assertEqual(self.run_raw('PreCompact', trigger='auto', agent_id='a1'), '')

    def test_fail_open_on_garbage(self):
        self.set_tokens(200_000)
        for raw in ('{', '', '[1]', 'null', '\xff'):
            self.assertEqual(self.run_hook('PostToolUse', raw=raw), {})
        self.transcript.write_bytes(b'\xff\x00not json\n{"usage"\n' * 10)
        self.assertEqual(self.run_hook('PostToolUse'), {})
        self.assertEqual(self.run_hook('PostToolUse', transcript_path=str(self.home / 'missing')), {})
        self.assertEqual(self.run_hook('PostToolUse', session_id='../x'), {})
        state = self.home / '.agents/state/budget'
        state.mkdir(parents=True)
        (state / 's1.json').write_bytes(b'\xff')
        self.set_tokens(155_000)
        self.assertIn('additionalContext', self.run_hook('PostToolUse')['hookSpecificOutput'])

    def test_reads_only_transcript_tail(self):
        filler = json.dumps({'type': 'user', 'message': {'content': 'x' * 1000}}) + '\n'
        self.transcript.write_text(filler * 2000)  # ~2 MB without usage
        self.assertEqual(self.run_hook('PostToolUse'), {})
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'assistant', 'message': {'usage': {
                'input_tokens': 185_000}}}) + '\n')
        self.assertIn('Context 185k:', self.run_hook('PostToolUse')['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
