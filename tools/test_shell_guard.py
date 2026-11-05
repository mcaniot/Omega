"""Tests for the shell_guard PreToolUse hook (native binary, hook JSON on stdin, decision JSON on stdout).

OMEGA_SHELL_GUARD_BINARY selects the guard under test (default: the deployed binary).
Hand-written cases state the policy; fixtures/shell_guard_golden.json.gz holds a few
thousand recorded (stdin, stdout) pairs, including malformed JSON, deep nesting,
mutated commands and several HOME values, that every build must reproduce byte for byte.
The guard derives its accepted paths from HOME, so every call sets it explicitly.
"""
import base64
import gzip
import json
import os
from pathlib import Path
import shlex
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor

BINARY = os.environ.get('OMEGA_SHELL_GUARD_BINARY', str(Path.home() / '.agents/tools/shell_guard'))
GOLDEN = Path(__file__).with_name('fixtures') / 'shell_guard_golden.json.gz'
HOME = '/home/tester'
NATIVE = f'{HOME}/.agents/tools/robot-exec/native/run'
LOCAL_READ = f'{HOME}/.agents/tools/robot-exec/local-read'


def guard(stdin, home=HOME, args=()):
    """(exit code, stdout, stderr) of one hook invocation."""
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8'}
    if home is not None:
        env['HOME'] = home
    done = subprocess.run([BINARY, *args], input=stdin, capture_output=True, env=env, timeout=30)
    return done.returncode, done.stdout, done.stderr


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.access(BINARY, os.X_OK):
            raise unittest.SkipTest(f'missing executable {BINARY}')

    def evaluate(self, payload):
        code, out, err = guard(json.dumps(payload).encode())
        self.assertEqual((code, err), (0, b''))
        self.assertLess(len(out), 1000)
        return json.loads(out)

    def call(self, command, **metadata):
        return self.evaluate({'tool_name': 'Bash', 'tool_input': {'command': command, **metadata}})

    def assertDenied(self, result):
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'PreToolUse')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertTrue(result['hookSpecificOutput']['permissionDecisionReason'])

    def test_run_and_inspect_leave_native_permissions_unchanged(self):
        for args in [('run', '--', 'pytest', '-q'), ('inspect', '/tmp/evidence')]:
            self.assertEqual(self.call(shlex.join([NATIVE, *args]), timeout=10, description='test'), {})

    def test_quoted_shell_script_is_only_wrapper_input(self):
        script = 'echo "$HOME"; printf x >&2; echo $(date)\nexit 3'
        self.assertEqual(self.call(shlex.join([NATIVE, 'run', '--shell', script])), {})

    def test_outer_shell_execution_is_denied(self):
        base = shlex.join([NATIVE, 'run', '--', 'echo', 'ok'])
        for extra in ['; echo leak', ' && echo leak', ' | cat', ' > /tmp/log',
                      ' 2>&1', ' &', '\necho leak', ' $(echo leak)', ' ' + chr(96) + 'echo leak' + chr(96)]:
            with self.subTest(extra=extra):
                self.assertDenied(self.call(base + extra))
        for prefix in ['FOO=x ', 'exec ', 'env ', 'echo leak; ']:
            self.assertDenied(self.call(prefix + base))

    def test_spoofed_paths_and_unknown_subcommands_are_denied(self):
        for argv in [[NATIVE + '.evil', 'run'],
                     [NATIVE.replace('/tools/', '/tools/../tools/'), 'run'],
                     ['/tmp/run', 'run'],
                     [NATIVE, 'unknown'],
                     [NATIVE],
                     ['python3', f'{HOME}/.agents/tools/run.py', 'run', '--', 'echo', 'ok'],
                     ['python3', '-c', 'print("leak")', NATIVE, 'run']]:
            with self.subTest(argv=argv):
                self.assertDenied(self.call(shlex.join(argv)))

    def test_denial_names_the_native_wrapper(self):
        reason = self.call('pytest -q')['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn(f'{NATIVE} run [--cwd DIR] [--timeout S] -- COMMAND ARGS', reason)

    def test_bad_payloads_and_unwrapped_commands_are_denied(self):
        for payload in [None, [], {}, {'tool_name': None}, {'tool_name': 'Bash'},
                        {'tool_name': 'Bash', 'tool_input': []}]:
            self.assertDenied(self.evaluate(payload))
        for command in [None, 1, '', ' ', 'pytest -q', 'echo "unterminated']:
            self.assertDenied(self.call(command))

    def test_other_tools_pass_without_an_approval(self):
        self.assertEqual(self.evaluate({'tool_name': 'Read'}), {})

    def test_canonical_quoted_metacharacters_are_inert(self):
        argv = [NATIVE, 'run', '--', 'echo', ';', '>', '$(date)', chr(96) + 'date' + chr(96), "a'b", 'line\nbreak']
        self.assertEqual(self.call(shlex.join(argv)), {})

    def test_inert_noncanonical_quoting_is_accepted(self):
        for command in [f"{NATIVE} run -- sed -i '47,106d' f.py",
                        f'{NATIVE} run -- rg -n "a|b(c)" src',
                        f'{NATIVE}  run\t-- echo "it\'s" \'"q"\' x\'y\'"z" \'\'',
                        f"{NATIVE} run --shell \"cd repo; ls | head\"",
                        f'"{NATIVE}" inspect /tmp/e --search "Traceback"']:
            with self.subTest(command=command):
                self.assertEqual(self.call(command), {})
        self.assertEqual(self.call(f'{NATIVE} run --shell "git push origin HEAD"')['hookSpecificOutput']
                         ['permissionDecision'], 'ask')
        for command in [f'{NATIVE} run -- echo "$HOME"', f'{NATIVE} run -- echo "`date`"',
                        f'{NATIVE} run -- echo "a\\"b"', f'{NATIVE} run -- echo "hi!"',
                        f"{NATIVE} run -- echo 'a''b'", f'{NATIVE} run -- echo ~/x', f'{NATIVE} run -- ls *.py',
                        f'{NATIVE} run -- echo {{a,b}}', f'{NATIVE} run -- echo "a\nb"\necho leak',
                        f'{NATIVE} run -- echo a\\ b']:
            with self.subTest(command=command):
                self.assertDenied(self.call(command))

    def decision(self, *inner, shell=None):
        argv = [NATIVE, 'run']
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

    def test_legacy_shell_flag_does_not_bypass_safety_policy(self):
        for script, expected in [('ssh robot ls', 'ask'), ('git push --force', 'deny')]:
            result = self.call(shlex.join([NATIVE, 'run', '--shell-keep-going', '--shell', script]))
            self.assertEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), expected)

    def test_inspect_is_never_policy_gated(self):
        self.assertEqual(self.call(shlex.join([NATIVE, 'inspect', '/tmp/sudo'])), {})

    def test_prune_apply_asks(self):
        self.assertEqual(self.call(shlex.join([NATIVE, 'prune'])), {})
        result = self.call(shlex.join([NATIVE, 'prune', '--apply']))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'ask')

    def test_allow_clean_grants_only_policy_clean_wrapper_calls(self):
        def decision(command, tool='Bash'):
            payload = json.dumps({'tool_name': tool, 'tool_input': {'command': command}}).encode()
            code, out, err = guard(payload, args=('--allow-clean',))
            self.assertEqual((code, err), (0, b''))
            return json.loads(out).get('hookSpecificOutput', {}).get('permissionDecision', 'pass')
        cases = [
            ([NATIVE, 'run', '--', 'cargo', 'build'], 'allow'),
            ([NATIVE, 'run', '--shell', 'make -j8 && pytest -q'], 'allow'),
            ([NATIVE, 'inspect', '/tmp/evidence'], 'allow'),
            ([NATIVE, 'prune'], 'allow'),
            ([LOCAL_READ, 'status'], 'allow'),
            ([NATIVE, 'prune', '--apply'], 'ask'),
            ([NATIVE, 'run', '--', 'git', 'push'], 'ask'),
            ([NATIVE, 'run', '--', 'rm', '-r', 'build'], 'ask'),
            ([NATIVE, 'run', '--shell', 'ros2 topic pub /cmd x'], 'ask'),
            ([NATIVE, 'run', '--', 'git', 'push', '--force'], 'deny'),
            ([NATIVE, 'run', '--', 'echo', '=ls'], 'deny'),
            ([LOCAL_READ, 'bogus'], 'deny'),
        ]
        for argv, expected in cases:
            self.assertEqual(decision(shlex.join(argv)), expected, argv)
        self.assertEqual(decision('pytest -q'), 'deny')
        self.assertEqual(decision('anything', tool='Read'), 'pass')

    def test_zsh_equals_argument_denied(self):
        self.assertDenied(self.call(shlex.join([NATIVE, 'run', '--', 'echo', '=ls'])))

    def test_local_read_forms(self):
        for argv in [[LOCAL_READ, 'status'], [LOCAL_READ, 'files'], [LOCAL_READ, 'text', 'a', 'b'],
                     [LOCAL_READ, 'find', 'a', 'b']]:
            self.assertEqual(self.call(shlex.join(argv)), {})
        for argv in [[LOCAL_READ], [LOCAL_READ, 'bogus'], [LOCAL_READ, 'status', 'x'], [LOCAL_READ, 'find', 'a', 'b', 'c']]:
            self.assertDenied(self.call(shlex.join(argv)))

    def test_internal_failure_fails_closed(self):
        # Nesting beyond the JSON parser's depth limit must deny, never pass through.
        code, out, _ = guard(b'{"tool_name":"Read","a":' + b'[' * 20000 + b']' * 20000 + b'}')
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertDenied(result)
        self.assertIn('Guard internal error', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_cli_outputs_valid_bounded_json_for_malformed_input(self):
        code, out, err = guard(b'{bad json')
        self.assertEqual((code, err), (0, b''))
        self.assertLess(len(out), 1000)
        self.assertDenied(json.loads(out))


class GoldenTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if not os.access(BINARY, os.X_OK):
            raise unittest.SkipTest(f'missing executable {BINARY}')
        with gzip.open(GOLDEN, 'rt', encoding='utf-8') as handle:
            cls.golden = json.load(handle)

    def test_recorded_decisions(self):
        for variant in self.golden['variants']:
            home = variant['home']
            stdins = [base64.b64decode(case[0]) for case in variant['cases']]
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda stdin: guard(stdin, home), stdins))
            bad = [(stdin[:200], result, case[1]) for stdin, result, case in zip(stdins, results, variant['cases'])
                   if result != (0, case[1].encode(), b'')]
            self.assertEqual(bad[:3], [], f'home={home!r}: {len(bad)} of {len(stdins)} differ')

    def test_fixture_covers_every_decision_kind(self):
        outcomes = set()
        for variant in self.golden['variants']:
            for _, stdout in variant['cases']:
                hook = json.loads(stdout).get('hookSpecificOutput', {})
                outcomes.add((hook.get('permissionDecision', 'pass'), hook.get('permissionDecisionReason', '').split(' Shell output')[0]))
        kinds = {kind for kind, _ in outcomes}
        self.assertEqual(kinds, {'deny', 'ask', 'pass'})
        for label in ['privilege escalation', 'git force push', 'rm -rf', 'raw device write',
                      'sensitive credential access', 'git history or remote change', 'git destructive change',
                      'recursive delete', 'remote host access', 'network upload', 'possible robot actuation',
                      'possible secret exposure', 'Invalid hook JSON.', 'Invalid hook input.',
                      'Missing shell command.', 'Malformed shell quoting.', 'Unwrapped or noncanonical shell command.',
                      'Unsupported read capability or arguments.', 'Argument starting with "=" is expanded by zsh.',
                      'Deleting old evidence requires explicit user approval.',
                      'Guard internal error (RecursionError).']:
            self.assertTrue(any(label in reason for _, reason in outcomes), label)


if __name__ == '__main__':
    unittest.main()
