"""Context budget hook contracts: soft reminder once, hard stop with a checkpoint allow-list."""
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
        payload = {'hook_event_name': event, 'session_id': 's1', 'transcript_path': str(self.transcript), **payload}
        done = subprocess.run([BINARY], input=raw if raw is not None else json.dumps(payload), text=True,
                              capture_output=True, check=True,
                              env={**{k: v for k, v in os.environ.items() if not k.startswith('OMEGA_CONTEXT_')},
                                   'HOME': str(self.home), **(env or {})})
        self.assertEqual(done.stderr, '')
        return json.loads(done.stdout)

    def pre(self, tool, **tool_input):
        return self.run_hook('PreToolUse', tool_name=tool, tool_input=tool_input)

    def denied(self, result):
        output = result.get('hookSpecificOutput', {})
        return output.get('permissionDecision') == 'deny'

    def test_soft_fires_once_per_crossing_and_rearms(self):
        self.assertEqual(self.run_hook('PostToolUse'), {})
        self.set_tokens(155_000)
        first = self.run_hook('PostToolUse')['hookSpecificOutput']
        self.assertEqual(first['hookEventName'], 'PostToolUse')
        self.assertEqual(first['additionalContext'],
                         'Context 155k/180k: checkpoint ~/.agents/tasks/active/<task>.md now.')
        self.assertEqual(self.run_hook('PostToolUse'), {})
        self.assertEqual(self.run_hook('UserPromptSubmit'), {})
        self.set_tokens(1000)
        self.assertEqual(self.run_hook('UserPromptSubmit'), {})
        self.set_tokens(160_000)
        self.assertEqual(self.run_hook('UserPromptSubmit')['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')

    def test_reset_events_rearm_soft(self):
        self.set_tokens(155_000)
        self.run_hook('PostToolUse')
        self.assertEqual(self.run_hook('SessionStart', source='compact'), {})
        self.assertIn('additionalContext', self.run_hook('PostToolUse')['hookSpecificOutput'])
        self.assertEqual(self.run_hook('PostCompact'), {})
        self.assertIn('additionalContext', self.run_hook('PostToolUse')['hookSpecificOutput'])

    def test_env_thresholds_and_sidechain_ignored(self):
        self.set_tokens(5000, sidechain_tokens=999_999)
        env = {'OMEGA_CONTEXT_SOFT': '4000', 'OMEGA_CONTEXT_HARD': '6000'}
        self.assertIn('5k/6k', self.run_hook('PostToolUse', env=env)['hookSpecificOutput']['additionalContext'])
        self.assertFalse(self.denied(self.run_hook('PreToolUse', env=env, tool_name='Bash', tool_input={})))

    def test_hard_denies_everything_but_checkpoint_tools(self):
        self.set_tokens(185_000)
        tasks = self.home / '.agents/tasks'
        reason = ("Context budget reached: auto handoff. 1) Checkpoint ~/.agents/tasks/active/<task>.md "
                  "(verified facts with file:line, current state, next action). 2) Run `'Ω-run' -- orca agent launch "
                  "--agent claude --prompt 'omega resume <task>' --json`. 3) Report the new agent handle and stop.")
        for tool, tool_input in (
                ('Bash', {'command': 'ls'}), ('Read', {'file_path': str(self.home / 'x.py')}),
                ('Edit', {'file_path': str(self.home / 'x.py')}), ('Grep', {'pattern': 'x'}),
                ('Write', {'file_path': str(tasks / '../escape.md')}),
                ('Edit', {'file_path': str(tasks) + '-other/x.md'}),
                ('Bash', {'command': "'Ω-run' --shell 'omega resume; rm -rf x'"}),
                ('Bash', {'command': "'Ω-run' -- rm -rf x"}),
                ('Bash', {'command': 'omega resume'}),
                ('Bash', {'command': "'Ω-run' -- orca agent list"}),
                ('Bash', {'command': "'Ω-run' -- orca agent launch --prompt x; rm -rf y"})):
            with self.subTest(tool=tool, tool_input=tool_input):
                result = self.pre(tool, **tool_input)
                self.assertEqual(result['hookSpecificOutput']['permissionDecisionReason'], reason)
                self.assertTrue(self.denied(result))
        for tool, tool_input in (
                ('Edit', {'file_path': str(tasks / 'active/t.md')}),
                ('Write', {'file_path': str(tasks / 'active/new.md')}),
                ('Read', {'file_path': str(tasks / 'active/t.md')}),
                ('Bash', {'command': "'Ω-run' -- omega resume"}),
                ('Bash', {'command': "'Ω-run' --shell 'omega resume t'"}),
                ('Bash', {'command': "'Ω-run' -- orca agent launch --agent claude --prompt 'omega resume t' --json"})):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertEqual(self.pre(tool, **tool_input), {})

    def test_below_hard_and_subagents_are_never_denied(self):
        self.assertEqual(self.pre('Bash', command='ls'), {})
        self.set_tokens(200_000)
        self.assertEqual(self.run_hook('PreToolUse', tool_name='Bash', tool_input={'command': 'ls'},
                                       agent_id='a1'), {})
        self.assertEqual(self.run_hook('PostToolUse', agent_id='a1'), {})

    def test_fail_open_on_garbage(self):
        self.set_tokens(200_000)
        for raw in ('{', '', '[1]', 'null', '\xff'):
            self.assertEqual(self.run_hook('PreToolUse', raw=raw), {})
        self.transcript.write_bytes(b'\xff\x00not json\n{"usage"\n' * 10)
        self.assertEqual(self.pre('Bash', command='ls'), {})
        self.assertEqual(self.run_hook('PreToolUse', tool_name='Bash', tool_input={},
                                       transcript_path=str(self.home / 'missing')), {})
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
        self.assertTrue(self.denied(self.pre('Bash', command='ls')))


if __name__ == '__main__':
    unittest.main()
