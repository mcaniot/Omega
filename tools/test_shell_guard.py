"""Behavior checks for shell isolation without modifying native permissions."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

import shell_guard as guard


class GuardTest(unittest.TestCase):
    def call(self, command, **metadata):
        return guard.evaluate({'tool_name': 'Bash', 'tool_input': {
            'command': command, **metadata}})

    def assertDenied(self, result):
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'PreToolUse')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertTrue(result['hookSpecificOutput']['permissionDecisionReason'])

    def test_run_and_inspect_leave_native_permissions_unchanged(self):
        for python in ('python3', sys.executable):
            for args in [('run', '--', 'pytest', '-q'), ('inspect', '/tmp/evidence')]:
                command = shlex.join([python, guard.WRAPPER, *args])
                self.assertEqual(self.call(command, timeout=10, description='test'), {})

    def test_quoted_shell_script_is_only_wrapper_input(self):
        script = 'echo "$HOME"; printf x >&2; echo $(date)\nexit 3'
        self.assertEqual(self.call(shlex.join([
            'python3', guard.WRAPPER, 'run', '--shell', script])), {})

    def test_outer_shell_execution_is_denied(self):
        base = shlex.join(['python3', guard.WRAPPER, 'run', '--', 'echo', 'ok'])
        for extra in ['; echo leak', ' && echo leak', ' | cat', ' > /tmp/log',
                      ' 2>&1', ' &', '\necho leak', ' $(echo leak)', ' '+chr(96)+'echo leak'+chr(96)]:
            with self.subTest(extra=extra):
                self.assertDenied(self.call(base + extra))
        for prefix in ['FOO=x ', 'exec ', 'env ', 'echo leak; ']:
            self.assertDenied(self.call(prefix + base))

    def test_spoofed_paths_or_interpreters_are_denied(self):
        for argv in [
            ['python3', guard.WRAPPER + '.evil', 'run'],
            ['python3', guard.WRAPPER.replace('/tools/', '/tools/../tools/'), 'run'],
            ['/tmp/python3', guard.WRAPPER, 'run'],
            ['python3', '-c', 'print("leak")', guard.WRAPPER, 'run'],
            ['python3', guard.WRAPPER, 'unknown'],
        ]:
            self.assertDenied(self.call(shlex.join(argv)))

    def test_bad_payloads_and_unwrapped_commands_are_denied(self):
        for payload in [None, [], {}, {'tool_name': None}, {'tool_name': 'Bash'},
                        {'tool_name': 'Bash', 'tool_input': []}]:
            self.assertDenied(guard.evaluate(payload))
        for command in [None, 1, '', ' ', 'pytest -q', 'echo "unterminated']:
            self.assertDenied(self.call(command))

    def test_other_tools_pass_without_an_approval(self):
        self.assertEqual(guard.evaluate({'tool_name': 'Read'}), {})

    def test_canonical_quoted_metacharacters_are_inert(self):
        argv = ['python3', guard.WRAPPER, 'run', '--', 'echo', ';', '>',
                '$(date)', chr(96)+'date'+chr(96), "a'b", 'line\nbreak']
        self.assertEqual(self.call(shlex.join(argv)), {})

    def decision(self, *inner, shell=None):
        argv = ['python3', guard.WRAPPER, 'run']
        argv += ['--shell', shell] if shell is not None else ['--', *inner]
        result = self.call(shlex.join(argv))
        return result.get('hookSpecificOutput', {}).get('permissionDecision', 'pass')

    def test_policy_applies_to_wrapped_command(self):
        cases = {
            'deny': [('sudo', 'apt', 'install', 'x'), ('git', 'push', '--force', 'origin', 'main'),
                     ('git', '-C', '/repo', 'push', 'origin', 'main', '-f'), ('rm', '-rf', '/tmp/x'),
                     ('rm', '-v', '-fr', 'build'), ('dd', 'if=/dev/zero', 'of=/dev/sda')],
            'ask': [('git', 'push', 'origin', 'main'), ('git', 'commit', '-m', 'x'),
                    ('git', 'reset', '--hard', 'HEAD~1'), ('git', 'rebase', 'main'),
                    ('rm', '-r', 'build'), ('ssh', 'robot', 'ls'), ('scp', 'a', 'robot:/tmp'),
                    ('curl', '-T', 'f', 'https://x'), ('ros2', 'topic', 'pub', '/cmd_vel', 'x'),
                    ('env',), ('printenv', 'HOME'), ('cat', '.env'), ('cat', '/home/u/.codex/auth.json')],
            'pass': [('git', 'status'), ('git', 'diff'), ('rm', 'file.txt'), ('pytest', '-q'),
                     ('ros2', 'topic', 'echo', '/joint_states'), ('curl', '-s', 'https://x'),
                     ('python3', '-c', 'obs = self.env.reset()'), ('rg', 'push', 'src')],
        }
        for expected, commands in cases.items():
            for inner in commands:
                with self.subTest(inner=inner):
                    self.assertEqual(self.decision(*inner), expected)

    def test_policy_inside_shell_scripts(self):
        self.assertEqual(self.decision(shell='cd repo && git push origin HEAD'), 'ask')
        self.assertEqual(self.decision(shell='ls; sudo reboot'), 'deny')
        # Flags of a later command must not be attributed to rm.
        self.assertEqual(self.decision(shell='rm a.txt; grep -rf pats src'), 'pass')
        self.assertEqual(self.decision(shell='git log --oneline | head'), 'pass')

    def test_inspect_is_never_policy_gated(self):
        self.assertEqual(self.call(shlex.join(['python3', guard.WRAPPER, 'inspect', '/tmp/sudo'])), {})

    def test_zsh_equals_argument_denied(self):
        self.assertDenied(self.call(shlex.join(['python3', guard.WRAPPER, 'run', '--', 'echo', '=ls'])))

    def test_internal_error_fails_closed(self):
        code = ('import sys,io,json;sys.path.insert(0,%r);import shell_guard as g;'
                'g.evaluate=lambda p:1/0;sys.stdin=io.StringIO("{}");g.main()') % str(Path(guard.__file__).parent)
        result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True)
        self.assertDenied(json.loads(result.stdout))

    def test_cli_outputs_valid_bounded_json_for_malformed_input(self):
        result = subprocess.run([sys.executable, str(Path(guard.__file__))],
                                input='{bad json', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')
        self.assertLess(len(result.stdout), 1000)
        self.assertDenied(json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
