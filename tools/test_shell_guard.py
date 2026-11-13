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
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

BINARY = os.environ.get('OMEGA_SHELL_GUARD_BINARY', str(Path.home() / '.agents/tools/shell_guard'))
GOLDEN = Path(__file__).with_name('fixtures') / 'shell_guard_golden.json.gz'
HOME = '/home/tester'
NATIVE = f'{HOME}/.agents/tools/robot-exec/native/run'


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

    def reason(self, command):
        result = self.call(command)
        self.assertDenied(result)
        return result['hookSpecificOutput']['permissionDecisionReason']

    def test_denial_offers_ready_to_run_rewrite(self):
        for command in ['pytest -q | head', "echo 'it''s' && ls"]:
            rewrite = shlex.join(['Ω-run', '--shell', command])
            self.assertIn(f'Ready-to-run: {rewrite} ', self.reason(command))
        self.assertIn("Ready-to-run: 'Ω-run' --shell 'echo \"unterminated'", self.reason('echo "unterminated'))

    def test_no_rewrite_for_policy_denied_or_oversized_commands(self):
        self.assertNotIn('Ready-to-run', self.reason('rm -rf /tmp/x'))
        self.assertNotIn('Ready-to-run', self.reason('echo ' + 'a' * 400))

    def test_no_nested_rewrite_for_miswritten_wrapper_calls(self):
        for command in ['Ω-run -- echo "$HOME"', f'{NATIVE} run -- ls | head', "'Ω-run' --shell 'echo \"unterminated"]:
            self.assertNotIn('Ready-to-run', self.reason(command), command)

    def test_evidence_reads_suggest_bounded_inspect(self):
        directory = f'{HOME}/.agents/tasks/evidence/20260101T000000-native-abcdef'
        expected = {
            f'cat {directory}/stdout.log': 'stdout',
            f'tail -n 5 {directory}/stderr.log | head': 'stderr',
            f'head {directory}/stdout': 'stdout',
            f'cat {directory}/result.json': 'auto',
        }
        for command, stream in expected.items():
            hint = shlex.join(['Ω-inspect', '--stream', stream, '--lines', '40', '--max-bytes', '7000', directory])
            self.assertIn(f'Ready-to-run: {hint} ', self.reason(command))

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
                        f'{NATIVE} run -- rg -n "\\.request\\(|a\\\\b" src --glob "*.py"',
                        f'{NATIVE}  run\t-- echo "it\'s" \'"q"\' x\'y\'"z" \'\'',
                        f"{NATIVE} run --shell \"cd repo; ls | head\"",
                        f'"{NATIVE}" inspect /tmp/e --search "Traceback"']:
            with self.subTest(command=command):
                self.assertEqual(self.call(command), {})
        self.assertEqual(self.call(f'{NATIVE} run --shell "git push origin HEAD"')['hookSpecificOutput']
                         ['permissionDecision'], 'ask')
        for command in [f'{NATIVE} run -- echo "$HOME"', f'{NATIVE} run -- echo "`date`"',
                        f'{NATIVE} run -- echo "a\\"b"', f'{NATIVE} run -- echo "a\\$b"', f'{NATIVE} run -- echo "a\\`b"',
                        f'{NATIVE} run -- echo "a\\!b"', f'{NATIVE} run -- echo "a\\\nb"', f'{NATIVE} run -- echo "a\\',
                        f'{NATIVE} run -- echo "hi!"',
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
                     ('rm', '-v', '-fr', 'build'), ('rm', '-r', 'build'),
                     ('rm', 'file.txt'), ('dd', 'if=/dev/zero', 'of=/dev/sda')],
            'ask': [('git', 'push', 'origin', 'main'), ('git', 'commit', '-m', 'x'),
                    ('git', 'reset', '--hard', 'HEAD~1'), ('git', 'rebase', 'main'),
                    ('git', 'rm', '-r', 'build'), ('ssh', 'robot', 'ls'), ('scp', 'a', 'robot:/tmp'),
                    ('curl', '-T', 'f', 'https://x'), ('ros2', 'topic', 'pub', '/cmd_vel', 'x'),
                    ('env',), ('printenv', 'HOME'), ('cat', '.env'), ('cat', '/home/u/.codex/auth.json')],
            'pass': [('git', 'status'), ('git', 'diff'), ('trash-put', '-r', 'build'), ('docker', 'run', '--rm', 'img'), ('pytest', '-q'),
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
        self.assertEqual(self.decision(shell='ls && rm a.txt'), 'deny')
        self.assertEqual(self.decision(shell='trash-put a.txt; grep -rf pats src'), 'pass')
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
            ([NATIVE, 'prune', '--apply'], 'ask'),
            ([NATIVE, 'run', '--', 'git', 'push'], 'ask'),
            ([NATIVE, 'run', '--', 'rm', '-r', 'build'], 'deny'),
            ([NATIVE, 'run', '--shell', 'cd x && rm out.txt'], 'deny'),
            ([NATIVE, 'run', '--shell', 'find . -name a -exec rm {} +'], 'deny'),
            ([NATIVE, 'run', '--', 'trash-put', 'build'], 'allow'),
            ([NATIVE, 'run', '--', 'git', 'rm', '--cached', 'a'], 'allow'),
            ([NATIVE, 'run', '--shell', 'ros2 topic pub /cmd x'], 'ask'),
            ([NATIVE, 'run', '--', 'git', 'push', '--force'], 'deny'),
            ([NATIVE, 'run', '--', 'echo', '=ls'], 'deny'),
            ([f'{HOME}/.agents/tools/robot-exec/local-read', 'status'], 'deny'),
            (['Ω-read', 'status'], 'deny'),
        ]
        for argv, expected in cases:
            self.assertEqual(decision(shlex.join(argv)), expected, argv)
        self.assertEqual(decision('pytest -q'), 'deny')
        self.assertEqual(decision('anything', tool='Read'), 'pass')

    def test_allow_clean_auto_wraps_unwrapped_policy_clean_commands(self):
        with tempfile.TemporaryDirectory() as home:
            native = Path(home, '.agents/tools/robot-exec/native/run')
            native.parent.mkdir(parents=True)
            native.write_text('#!/bin/sh\n')
            native.chmod(0o700)
            Path(home, 'bin').mkdir()
            Path(home, 'bin/Ω-run').symlink_to(native)

            def output(command, args=('--allow-clean',), tool='Bash', **extra):
                payload = json.dumps({'tool_name': tool, 'tool_input': {'command': command, **extra}}).encode()
                done = subprocess.run([BINARY, *args], input=payload, capture_output=True, timeout=30,
                                      env={'PATH': f'{home}/bin:/usr/bin:/bin', 'HOME': home})
                self.assertEqual((done.returncode, done.stderr), (0, b''))
                return json.loads(done.stdout)['hookSpecificOutput']

            wrapped = output("cd x && make 'a b'", timeout=5000, description='d')
            self.assertEqual(wrapped['permissionDecision'], 'allow')
            self.assertEqual(wrapped['updatedInput'], {'command': shlex.join(['Ω-run', '--shell', "cd x && make 'a b'"]),
                                                       'timeout': 5000, 'description': 'd'})
            self.assertIn('Ω-run --shell', wrapped['additionalContext'])
            heredoc = "python3 - <<'EOF'\n" + 'print("x")\n' * 60 + 'EOF'
            self.assertEqual(output(heredoc)['updatedInput'], {'command': shlex.join(['Ω-run', '--shell', heredoc])})
            for command in ('git push', 'rm -r build', 'ros2 topic pub /cmd x', 'echo "unterminated',
                            'Ω-run --shell "echo $HOME"', 'omega-run -- "ls"', 'echo ' + 'a' * 17000):
                denied = output(command)
                self.assertEqual(denied['permissionDecision'], 'deny', command)
                self.assertNotIn('updatedInput', denied, command)
            self.assertEqual(output('ls', args=())['permissionDecision'], 'deny')  # Codex: no rewrite
            self.assertEqual(output('ls', tool='exec_command')['permissionDecision'], 'deny')

    def test_zsh_equals_argument_denied(self):
        self.assertDenied(self.call(shlex.join([NATIVE, 'run', '--', 'echo', '=ls'])))

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
        for label in ['privilege escalation', 'git force push', 'rm. Delete with trash-put', 'raw device write',
                      'sensitive credential access', 'git history or remote change', 'git destructive change',
                      'remote host access', 'network upload', 'possible robot actuation',
                      'possible secret exposure', 'Invalid hook JSON.', 'Invalid hook input.',
                      'Missing shell command.', 'Malformed shell quoting.', 'Unwrapped or noncanonical shell command.',
                      'Argument starting with "=" is expanded by zsh.',
                      'Deleting old evidence requires explicit user approval.',
                      'Guard internal error (RecursionError).']:
            self.assertTrue(any(label in reason for _, reason in outcomes), label)


if __name__ == '__main__':
    unittest.main()
