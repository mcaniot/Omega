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
        # The omega CLI is a plain command, not a miswritten wrapper call.
        for command in ['pytest -q | head', "echo 'it''s' && ls", 'omega --help; omega --version']:
            rewrite = shlex.join(['Ω-run', '--shell', command])
            self.assertIn(f'Ready-to-run: {rewrite} ', self.reason(command))
        self.assertIn("Ready-to-run: 'Ω-run' --shell 'echo \"unterminated'", self.reason('echo "unterminated'))

    def test_no_rewrite_for_policy_denied_or_oversized_commands(self):
        self.assertNotIn('Ready-to-run', self.reason('rm -rf /tmp/x'))
        self.assertNotIn('Ready-to-run', self.reason('echo ' + 'a' * 400))

    def test_no_nested_rewrite_for_miswritten_wrapper_calls(self):
        for command in ['Ω-run -- echo "$HOME"', f'{NATIVE} run -- ls | head', "'Ω-run' --shell 'echo \"unterminated"]:
            self.assertNotIn('Ready-to-run', self.reason(command), command)

    def test_miswritten_wrapper_calls_name_their_fix(self):
        evidence = f'{HOME}/.agents/tasks/evidence/20260101T000000-native-abcdef'
        expected = {
            f"'Ω-inspect' {evidence} --offset 0; 'Ω-inspect' {evidence} --offset 2900": 'Ω-inspect is already bounded',
            f'{NATIVE} run -- ls | head': 'Nothing may precede or follow the wrapper',
            f'{NATIVE} inspect {evidence} | head -5': 'Ω-inspect is already bounded',
            '\'Ω-run\' -- python3 -c "print(1, \'$x||\')"': 'Double quotes keep $, backquote and ! live',
            "cat > /tmp/s.py <<'EOF'\n# don't\n" + 'x = 1\n' * 60 + 'EOF': 'Heredoc: create the file',
        }
        for command, fix in expected.items():
            self.assertIn(fix, self.reason(command), command)
        # A ready-to-run form, a spoofed path and an unquoted expansion keep the generic guidance;
        # operators and "<<" inside a quoted argument are not outer operators or a heredoc.
        fixes = set(expected.values())
        quoted = {
            f"{NATIVE} run -- python3 -c 'print(1 << 2); x = \"<<\";' \"$HOME\"": 'Double quotes keep $, backquote and ! live',
            "cat > /tmp/s.py <<'EOF'\n# don't\nEOF": 'Ready-to-run',
            '\'Ω-run\' -- python3 -c "print(1 != 2, \'||\')"': 'Ready-to-run',  # `!` is literal: requoted
            "'Ω-run' --shell 'python3 - <<\"EOF\"\nprint(\"it's\")\nEOF'": 'Ready-to-run',
        }
        for command, fix in quoted.items():
            reason = self.reason(command)
            self.assertIn(fix, reason, command)
            self.assertEqual({other for other in fixes if other in reason}, {fix} & fixes, command)
        for command in ['pytest -q | head', 'echo "unterminated', f'{NATIVE}.evil run', f'{NATIVE} run -- echo $(date)']:
            reason = self.reason(command)
            for fix in expected.values():
                self.assertNotIn(fix, reason, command)

    def test_pkill_full_pattern_must_not_match_its_own_command_line(self):
        for argv in (['pkill', '-f', 'datagen.collect'], ['pkill', '-9', '-f', 'x'], ['pkill', '--full', 'train.py']):
            self.assertIn('pkill -f self-match', self.reason(shlex.join([NATIVE, 'run', '--', *argv])))
        script = 'pkill -f "a b" || true; sleep 1'
        self.assertIn('pkill -f self-match', self.reason(shlex.join([NATIVE, 'run', '--shell', script])))
        for argv in (['pkill', '-f', '[d]atagen.collect'], ['pkill', '-f', '^python3 -m datagen'],
                     ['pkill', '-x', 'python3'], ['pgrep', '-af', 'datagen'],
                     ['echo', 'the guard denies pkill -f name in prose']):
            self.assertEqual(self.call(shlex.join([NATIVE, 'run', '--', *argv])), {})

    def test_evidence_reads_suggest_bounded_inspect(self):
        directory = f'{HOME}/.agents/tasks/evidence/20260101T000000-native-abcdef'
        expected = {
            f'cat {directory}/stdout.log': 'stdout',
            f'tail -n 5 {directory}/stderr.log | head': 'stderr',
            f'head {directory}/stdout': 'stdout',
            f'cat {directory}/result.json': 'auto',
            f'cat {directory}/stdout* 2>/dev/null | sed -n 1,299p': 'stdout',
            f'head {directory}/stderr*': 'stderr',
            f'cat {directory}/*.log': 'auto',
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

    def test_secret_exposure_needs_command_position(self):
        # Recorded false positives: an rg alternation, a Python call, a subcommand, RST markup.
        for text in ['rg -n "place\\(env|x" src', 'x = defaultdict(set)', 'uv run datagen env',
                     'env.step(act(env))', 'see ``env`` here', 'on every set)']:
            self.assertNotIn('permissionDecision', json.dumps(self.call(shlex.join([NATIVE, 'run', '--shell', text]))), text)
        for text in ['env', 'ls; env | grep KEY', 'echo $(printenv)', '(set)', 'a && set', 'x=`env`']:
            self.assertEqual(self.call(shlex.join([NATIVE, 'run', '--shell', text]))['hookSpecificOutput']['permissionDecision'],
                             'ask', text)

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

    def test_inspect_may_feed_a_read_only_filter(self):
        # A trusted `Ω-inspect` on PATH (a real HOME is needed: the guard checks the alias target).
        with tempfile.TemporaryDirectory() as home:
            native = Path(home, '.agents/tools/robot-exec/native/run')
            native.parent.mkdir(parents=True)
            native.symlink_to('/bin/true')
            bin_dir = Path(home, 'bin')
            bin_dir.mkdir()
            (bin_dir / 'Ω-inspect').symlink_to(native)

            def decide(command):
                payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}}).encode()
                done = subprocess.run([BINARY], input=payload, capture_output=True, timeout=30,
                                      env={'PATH': f'{bin_dir}:/usr/bin:/bin', 'HOME': home, 'LC_ALL': 'en_US.UTF-8'})
                self.assertEqual((done.returncode, done.stderr), (0, b''))
                return json.loads(done.stdout)
            for command in ["'Ω-inspect' /tmp/e --search error | head -5", "'Ω-inspect' /tmp/e 2>&1 | tail -20",
                            "'Ω-inspect' /tmp/e | grep -c foo", "'Ω-inspect' /tmp/e --search 'a|b' | wc -l",
                            "'Ω-inspect' /tmp/e | rg 'x y'", "'Ω-inspect' /tmp/e --search 'error' 2>&1 | head -30",
                            "'Ω-inspect' /tmp/e --search 'nan' | grep -n 'loss'", 'Ω-inspect /tmp/e --search "a|b"  |  head -3',
                            # Recorded: denied for `cut` or a second filter, each resent without them.
                            "'Ω-inspect' /tmp/e --lines 90 | cut -c1-220",
                            "'Ω-inspect' /tmp/e --search 'a|b' 2>/dev/null | cut -c1-700 | head -n 45"]:
                with self.subTest(command=command):
                    self.assertEqual(decide(command), {})
            for command in ["'Ω-inspect' /tmp/e | python3 -c 'import os'", "'Ω-inspect' /tmp/e | head -5 | cat",
                            "'Ω-inspect' /tmp/e | cut -c1-9 | sh", "'Ω-inspect' /tmp/e 2>/tmp/x | cut -c1-9",
                            "'Ω-inspect' /tmp/e | head -5; ls", "'Ω-inspect' /tmp/e | head > /tmp/x",
                            "'Ω-inspect' /tmp/e | rg --pre ls x", "ls | head", "'Ω-inspect' /tmp/e; ls | head",
                            "'Ω-inspect' /tmp/e --search 'error' | head -3; ls", "'Ω-inspect' /tmp/e --search 'x' | head $(ls)",
                            "'Ω-inspect' /tmp/e --search 'x' | head -3 > /tmp/x",
                            "'Ω-inspect' /tmp/e --search \"$(ls)\" | head", "'Ω-inspect' /tmp/e --search 'x' || head"]:
                with self.subTest(command=command):
                    self.assertEqual(decide(command)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_prune_apply_asks(self):
        self.assertEqual(self.call(shlex.join([NATIVE, 'prune'])), {})
        result = self.call(shlex.join([NATIVE, 'prune', '--apply']))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'ask')

    def test_vibe_protocol_denies_asks_and_rewrites_in_its_own_shape(self):
        def vibe(command, tool='file_system.bash'):
            payload = json.dumps({'hook_event_name': 'pre_tool', 'tool_name': tool,
                                  'tool_input': {'command': command, 'timeout': 30}}).encode()
            code, out, err = guard(payload, args=('--allow-clean', '--vibe'))
            self.assertEqual((code, err), (0, b''))
            result = json.loads(out)
            self.assertNotIn('hookSpecificOutput', result)  # Vibe would read it as an allow
            return result
        self.assertEqual(vibe(shlex.join([NATIVE, 'run', '--', 'ls'])), {})
        for command in (shlex.join([NATIVE, 'run', '--', 'rm', '-r', 'build']), shlex.join([NATIVE, 'prune', '--apply'])):
            result = vibe(command)
            self.assertEqual(result['decision'], 'deny', command)
            self.assertTrue(result['reason'])
        self.assertIn('ask the user', vibe(shlex.join([NATIVE, 'prune', '--apply']))['reason'])
        # Every tool the hook matcher sends is a shell tool, whatever its qualified name.
        self.assertEqual(vibe('git push --force', tool='file_system.process_start')['decision'], 'deny')
        code, out, _ = guard(b'{', args=('--vibe',))
        self.assertEqual(json.loads(out)['decision'], 'deny')

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
        # Batched wrapper calls: allowed only when every call is clean on its own.
        run = lambda *argv: shlex.join([NATIVE, 'run', *argv])  # noqa: E731
        allowed = [f"{run('--shell', 'a; b')}; {run('--', 'ls')}", f"{run('--', 'ls')} && {run('--', 'pwd')}",
                   f"{run('--', 'ls')}; {shlex.join([NATIVE, 'inspect', '/tmp/e'])}",
                   f"{run('--', 'ls')} || {run('--', 'pwd')}"]
        denied = [f"{run('--', 'ls')}; {run('--', 'rm', 'x')}", f"{run('--', 'ls')}; ls", f"{run('--', 'ls')} | head",
                  f"{run('--', 'ls')} & {run('--', 'pwd')}", f"{run('--', 'ls')} || {run('--', 'rm', 'x')}",
                  f"{run('--', 'ls')}; {run('--', 'git', 'push')}", f"{run('--', 'ls')}; echo $({run('--', 'pwd')})"]
        for command in allowed:
            self.assertEqual(decision(command), 'allow', command)
        for command in denied:
            self.assertNotEqual(decision(command), 'allow', command)

    def test_every_text_the_executor_may_run_is_checked(self):
        def decision(command):
            for alias, sub in (('Ω-run', 'run'), ('Ω-inspect', 'inspect'), ('Ω-prune', 'prune')):
                command = command.replace(alias, f'{NATIVE} {sub}')
            payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}}).encode()
            code, out, err = guard(payload, args=('--allow-clean',))
            self.assertEqual((code, err), (0, b''))
            return json.loads(out).get('hookSpecificOutput', {}).get('permissionDecision', 'pass')
        key = '/home/tester/.' + 'ssh/id_' + 'rsa'
        cases = [
            ("Ω-run -- bash -c 'sudo id' --shell x", 'deny'),
            ("Ω-run bash -c 'git push --force' --shell x", 'ask'),
            ("Ω-run '--shell=sudo id' --", 'deny'),
            ('Ω-run --shell-executable --shell -- sudo id', 'deny'),
            (f'Ω-inspect /tmp/x | head -n 5 {key}', 'deny'),
            (f'Ω-run -- head -n 5 {key}', 'ask'),
            ('Ω-prune --appl', 'ask'), ('Ω-prune --apply=1', 'ask'), ('Ω-prune', 'allow'),
            ('Ω-run -- git -P push --force', 'deny'), ('Ω-run -- git -P commit -m x', 'ask'),
            ('Ω-run -- git --work-tree . reset --hard', 'ask'), ('Ω-run -- git --no-pager log', 'allow'),
            ('Ω-run -- git -C x status', 'allow'),
            ('Ω-run git commit -m x -- f', 'ask'), ('Ω-run rm -rf /tmp/x -- y', 'deny'),
            ('Ω-run --cwd /home/u/.ssh -- cat config', 'ask'), ('Ω-run -- tar czf x.tgz /home/u/.ssh', 'ask'),
            ('Ω-run -- Ω-prune --apply', 'ask'), ("Ω-run --shell 'Ω-prune --apply'", 'ask'),
            (f'Ω-run -- {NATIVE} prune --app', 'ask'), ("Ω-run -- sh -c 'ls; Ω-prune --apply'", 'ask'),
            ('Ω-run -- Ω-prune --dry-run', 'allow'),
            ('Ω-run --timeout 5 pkill -f name', 'deny'), ('Ω-run --fail-fast --cwd /tmp pkill -f name', 'deny'),
            ("Ω-run --shell 'which git\nrm important.txt'", 'deny'), ('Ω-run -- git rm x', 'allow'),
            ('Ω-inspect /x/y | rg --hostname-bin /tmp/x foo', 'deny'),
            ('Ω-run -- git stash', 'ask'), ("Ω-run --shell 'git stash pop'", 'ask'), ('Ω-run -- git stash -u', 'ask'),
            ('Ω-run -- git stash list', 'allow'),
            ('Ω-inspect /x/y | head -n 5 /etc/passwd', 'deny'), ('Ω-inspect /x/y | grep -f /etc/passwd', 'deny'),
            ('Ω-inspect /x/y | grep -rn foo', 'deny'), ('Ω-inspect /x/y | grep -e a /etc/passwd', 'deny'),
            ("Ω-run --shell 'git --no-pager log; rm important.txt'", 'deny'),
            ('Ω-run --native-only --timeout 5 pkill -f name', 'deny'),
            ('Ω-run --shell-executable -- -- pkill -f name', 'deny'),
        ]
        for command, expected in cases:
            self.assertEqual(decision(command), expected, command)

    def test_rewrites_never_drop_part_of_the_command(self):
        with tempfile.TemporaryDirectory() as home:
            native = Path(home, '.agents/tools/robot-exec/native/run')
            native.parent.mkdir(parents=True)
            native.write_text('#!/bin/sh\n')
            native.chmod(0o700)
            Path(home, 'bin').mkdir()
            for name in ('Ω-run', 'Ω-inspect'):
                Path(home, 'bin', name).symlink_to(native)

            def result(command):
                payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}}).encode()
                done = subprocess.run([BINARY, '--allow-clean'], input=payload, capture_output=True, timeout=30,
                                      env={'PATH': f'{home}/bin:/usr/bin:/bin', 'HOME': home})
                return json.loads(done.stdout)['hookSpecificOutput']
            for command in ('cat ~/.agents/tasks/evidence/x/stdout.log && echo done',
                            'cat ~/.agents/tasks/evidence/x/stdout.log > /tmp/out.txt'):
                self.assertEqual(result(command)['updatedInput']['command'], shlex.join(['Ω-run', '--shell', command]))
            for command in ('cat ~/.agents/tasks/evidence/x/stdout.log\npytest -q tests',
                            'cat ~/.agents/tasks/evidence/x/stdout.log | python3 parse.py',
                            'cat ~/.agents/tasks/evidence/x/stdout.log | grep -c ERROR',
                            'cat ~/.agents/tasks/evidence/x/stdout.log | tee f',
                            'cat ~/.agents/tasks/evidence/x/stdout.log /tmp/other.txt',
                            'cat ~/.agents/tasks/evidence/2026*/stdout.log'):
                self.assertEqual(result(command)['updatedInput']['command'], shlex.join(['Ω-run', '--shell', command]))
            inspect = shlex.split(result('cat ~/.agents/tasks/evidence/x/stdout.log 2>&1')['updatedInput']['command'])
            self.assertEqual((inspect[0], inspect[-1]), ('Ω-inspect', f'{home}/.agents/tasks/evidence/x'))
            reason = result("Ω-run --shell='make build' 2>/dev/null; Ω-run -- ls")
            self.assertNotIn("Ready-to-run: 'Ω-run' -- ls", reason['permissionDecisionReason'])
            self.assertNotIn('Ready-to-run: ', result('Ω-run --shell-keep-going; true')['permissionDecisionReason'])
            self.assertEqual(result("'Ω-run' -- false || ls")['permissionDecision'], 'allow')

    def test_allow_clean_auto_wraps_unwrapped_policy_clean_commands(self):
        with tempfile.TemporaryDirectory() as home:
            native = Path(home, '.agents/tools/robot-exec/native/run')
            native.parent.mkdir(parents=True)
            native.write_text('#!/bin/sh\n')
            native.chmod(0o700)
            Path(home, 'bin').mkdir()
            Path(home, 'bin/Ω-run').symlink_to(native)
            Path(home, 'bin/Ω-inspect').symlink_to(native)

            def output(command, args=('--allow-clean',), tool='Bash', top=(), **extra):
                payload = json.dumps({'tool_name': tool, 'tool_input': {'command': command, **extra}, **dict(top)}).encode()
                done = subprocess.run([BINARY, *args], input=payload, capture_output=True, timeout=30,
                                      env={'PATH': f'{home}/bin:/usr/bin:/bin', 'HOME': home})
                self.assertEqual((done.returncode, done.stderr), (0, b''))
                return json.loads(done.stdout)['hookSpecificOutput']

            wrapped = output("cd x && make 'a b'", timeout=5000, description='d')
            self.assertEqual(wrapped['permissionDecision'], 'allow')
            self.assertEqual(wrapped['updatedInput'], {'command': shlex.join(['Ω-run', '--shell', "cd x && make 'a b'"]),
                                                       'timeout': 5000, 'description': 'd'})
            self.assertIn('Ω-run --shell', wrapped['additionalContext'])
            # A cd before the wrapper moves into --cwd instead of nesting one result JSON in another.
            for command, moved in [("cd ../a && 'Ω-run' --shell 'ls | head -3'",
                                    ['Ω-run', '--cwd', '../a', '--shell', 'ls | head -3']),
                                   ('cd /tmp/x && Ω-run -- make test', ['Ω-run', '--cwd', '/tmp/x', '--', 'make', 'test'])]:
                self.assertEqual(output(command)['updatedInput'], {'command': shlex.join(moved)}, command)
            for command in ("cd a && 'Ω-run' --cwd b -- ls", "cd a && 'Ω-run' -- ls | head", 'cd a && Ω-run -- ls; ls',
                            'cd "$X" && Ω-run -- ls'):
                self.assertNotIn('--cwd a', output(command).get('updatedInput', {}).get('command', ''), command)
            heredoc = "python3 - <<'EOF'\n" + 'print("x")\n' * 60 + 'EOF'
            self.assertEqual(output(heredoc)['updatedInput'], {'command': shlex.join(['Ω-run', '--shell', heredoc])})
            # Recorded: 39 calls of the retired Ω-read since 2026-11-09, each a failed round trip.
            # Plain file arguments now run as a bounded head instead.
            for command, files in [("'Ω-read' /x/README.md", ['/x/README.md']), ('Ω-read a.py b.py', ['a.py', 'b.py'])]:
                self.assertEqual(output(command)['updatedInput'],
                                 {'command': shlex.join(['Ω-run', '--shell', shlex.join(['head', '-n', '200', '--', *files])])},
                                 command)
            # `~` and globs would stay literal once quoted; a newline or comment is not a file list.
            for command in ("'Ω-read' --help", "'Ω-read' a.py 2>&1", "'Ω-read'", 'Ω-read ~/notes.md', 'Ω-read *.py',
                            'Ω-read a.py\ntouch x', 'Ω-read a.py # c'):
                self.assertNotIn('updatedInput', output(command), command)
            # Recorded: 130 heredocs denied as malformed quoting because the body held an apostrophe,
            # each costing a round trip to write a file instead. The body is data: auto-wrapped now.
            for heredoc in ("python3 - <<'EOF'\ns = \"dataset's range\"\nprint(s)\nEOF",
                            "cd /tmp && mkdir -p d && cat > d/f <<'EOF'\n#!/bin/bash\necho \"it's $1\"\nEOF\nchmod +x d/f",
                            'cat <<-EOF\n\tdon\'t\n\tEOF'):
                self.assertEqual(output(heredoc)['updatedInput'], {'command': shlex.join(['Ω-run', '--shell', heredoc])},
                                 heredoc)
            # The policy reads the body: a push is asked for, wrapped.
            pushed = "python3 - <<'EOF'\nimport os\nos.system('git push --force')\nit's\nEOF"
            self.assertEqual(output(pushed)['permissionDecision'], 'ask')
            for command in ("python3 - <<'EOF'\nit's\n",  # unterminated body
                            "echo it's <<'EOF'\nx\nEOF",  # unbalanced quote outside the body
                            "'Ω-run' --shell 'git push --force origin 'main''",  # literal script, policy still applies
                            "'Ω-run' --shell 'echo a' && 'Ω-run' --shell-keep-going 'x'"):
                self.assertNotIn('updatedInput', output(command), command)
            # Recorded: 107 wrapper calls whose single-quoted script held apostrophes were denied (the
            # shell would cut the script); the text between the outer quotes is run as the script.
            for options, script in [(['--cwd', '/tmp'], "python3 - <<\"EOF\"\ns.replace('a', 'b')\nEOF"),
                                    ([], "rg -n 'x y' src | head"),
                                    (['--timeout', '60'], "echo don't")]:
                command = shlex.join(['Ω-run', *options]) + " --shell '" + script + "'"
                self.assertEqual(output(command)['updatedInput'],
                                 {'command': shlex.join(['Ω-run', *options, '--shell', script])}, command)
            # Recorded: 26 wrapper calls denied for what sat outside them (an outer redirection or
            # filter, a trailing `; true`, a plain command after the call), each resent by hand.
            for command, fixed in [
                    ("'Ω-run' --shell 'cat a; cat b' > /tmp/out.txt", "'Ω-run' --shell '{ cat a; cat b\n} > /tmp/out.txt'"),
                    ("'Ω-run' --cwd /tmp --shell 'make' 2>&1 | head -5", "'Ω-run' --cwd /tmp --shell 'make 2>&1 | head -5'"),
                    ("'Ω-run' -- rg -n x src | cut -c1-80", "'Ω-run' --shell 'rg -n x src | cut -c1-80'"),
                    ("'Ω-run' --shell 'rg x | head' ; true", "'Ω-run' --shell 'rg x | head'"),
                    ("'Ω-run' -- ls || true", "'Ω-run' -- ls"),
                    ("'Ω-run' --shell 'ls'; sed -n '1,80p' f.yml", "'Ω-run' --shell 'ls'; 'Ω-run' --shell 'sed -n '\"'\"'1,80p'\"'\"' f.yml'"),
                    ("'Ω-run' -- make 2>&1 | tail -3; 'Ω-inspect' /e --lines 9 | head",
                     "'Ω-run' --shell 'make 2>&1 | tail -3'; 'Ω-inspect' /e --lines 9 | head"),
                    # Recorded 8 times: a call with options only, as if it set a mode for the next.
                    ("'Ω-run' --shell-keep-going 2>/dev/null; 'Ω-run' --shell 'ls'", "'Ω-run' --shell 'ls'"),
                    ("'Ω-inspect' --help 2>&1 | head -20; ls /tmp", "'Ω-inspect' --help 2>&1 | head -20; 'Ω-run' --shell 'ls /tmp'")]:
                wrapped = output(command)
                self.assertEqual(wrapped.get('updatedInput'), {'command': fixed}, command)
                self.assertLess(len(fixed), len(command) + 40)  # no nested result, no extra escaping
            # Recorded: 34 argv wrapper calls denied for quoting the shell would not take as written
            # (`!` or `\"` in double quotes, `(` or `\;` outside quotes) with no ready-to-run form.
            for command, argv in [('Ω-run -- rg -n "a(?!b)" -g "!x/**"', ['rg', '-n', 'a(?!b)', '-g', '!x/**']),
                                  ('Ω-run -- rg -F -e self.reset() src', ['rg', '-F', '-e', 'self.reset()', 'src']),
                                  ('Ω-run -- sed -n 45,60p\\;125,150p f', ['sed', '-n', '45,60p;125,150p', 'f']),
                                  ('Ω-run -- python3 -c "print(\\"x\\")"', ['python3', '-c', 'print("x")']),
                                  ('Ω-run -- rg -e ^diff -e def\\ f x', ['rg', '-e', '^diff', '-e', 'def f', 'x']),
                                  ('Ω-run -- rg -n x -g !*.md --glob=*.py --include *.h', ['rg', '-n', 'x', '-g', '!*.md',
                                                                                        '--glob=*.py', '--include', '*.h'])]:
                self.assertEqual(output(command).get('updatedInput'), {'command': shlex.join(['Ω-run', '--', *argv])},
                                 command)
            for command in ('Ω-run -- rg "$1" x', 'Ω-run -- ls ~/x', 'Ω-run -- ls *.py', 'Ω-run -- rg -g x *.py', 'Ω-run -- rg "a\\$b"',
                            'Ω-run -- echo "a\\`b"', 'Ω-run -- ls {a,b}', 'Ω-run -- echo a # b', 'Ω-run -- echo "a\\\nb"',
                            'Ω-run -- rg "!x" | head; rm -r y'):
                self.assertNotIn('updatedInput', output(command), command)
            # Recorded: 11 scripts escaped their heredoc delimiter ('"'"'EOF'"'"') but left a raw
            # apostrophe in the body; the body is data, so the decoded script is what was meant.
            script = "cd /tmp && python3 - <<'EOF'\ns = \"the episode's end\"\nprint('x')\nEOF\ngit diff --stat"
            escaped = script.replace("'EOF'", "'\"'\"'EOF'\"'\"'").replace("print('x')", "print('\"'\"'x'\"'\"')")
            command = f"'Ω-run' --shell '{escaped}'"
            self.assertEqual(output(command).get('updatedInput'), {'command': shlex.join(['Ω-run', '--shell', script])})
            # Recorded: options written after the script (`--shell '…' --cwd DIR`) move before it.
            script = "python3 - <<\"EOF\"\nprint('x')\nEOF"
            self.assertEqual(output(f"'Ω-run' --shell '{script}' --cwd /tmp").get('updatedInput'),
                             {'command': shlex.join(['Ω-run', '--cwd', '/tmp', '--shell', script])})
            script = "print(open('a').read())"  # recorded with --shell-executable python3
            self.assertEqual(output(f"'Ω-run' --shell-executable /usr/bin/python3 --shell '{script}'").get('updatedInput'),
                             {'command': shlex.join(['Ω-run', '--shell-executable', '/usr/bin/python3', '--shell', script])})
            for script in ("echo it's\npython3 - <<'\"'\"'EOF'\"'\"'\nx\nEOF",  # slip outside the body
                           "python3 - <<'\"'\"'EOF'\"'\"'\nit's\n",  # unterminated body
                           "python3 - <<'\"'\"'EOF'\"'\"'\nprint('\"'\"'x'\"'\"')\nEOF"):  # no slip: already right
                self.assertNotIn('updatedInput', output(f"'Ω-run' --shell '{script}'"), script)
            # Recorded: 10 argv wrapper calls fed by a heredoc outside them (the wrapper passes no stdin).
            body = "s = open('a').read()\nprint(\"x\")"
            for options, delimiter in [(['--cwd', '/tmp'], "'EOF'"), ([], 'PY'), ([], '"E"')]:
                command = shlex.join(['Ω-run', *options, '--', 'python3', '-']) + f" <<{delimiter}\n{body}\n{delimiter.strip(chr(34) + chr(39))}"
                script = f"python3 - <<{delimiter}\n{body}\n{delimiter.strip(chr(34) + chr(39))}"
                self.assertEqual(output(command).get('updatedInput'),
                                 {'command': shlex.join(['Ω-run', *options, '--shell', script])}, command)
            for command in ("'Ω-run' -- python3 - <<'EOF'\nx\nEOF\nrm -r y",  # runs after the body
                            "'Ω-run' -- python3 - <<'EOF'\nx\nEOF\nls\nEOF",  # second command after the body
                            "'Ω-run' -- python3 - <<'EOF' | head\nx\nEOF"):
                self.assertNotIn('updatedInput', output(command), command)
            pushed = "'Ω-run' -- python3 - <<'EOF'\nimport os; os.system('git push --force')\nEOF"
            self.assertEqual(output(pushed)['permissionDecision'], 'ask')  # fixed form, asked for
            for command in ("'Ω-run' -- ls; cd /tmp; 'Ω-run' -- ls",  # cd would not reach the next call
                            "'Ω-run' --shell 'ls' & 'Ω-run' -- ls",  # background job
                            "'Ω-run' --shell 'ls' | rm -r x",  # policy reads the moved filter
                            "'Ω-run' -- ls; git push --force"):
                self.assertNotIn('updatedInput', output(command), command)
            for command in ('rm -r build', 'echo "unterminated',
                            'Ω-run --shell "echo $HOME"', 'omega-run -- "ls"', 'echo ' + 'a' * 17000):
                denied = output(command)
                self.assertEqual(denied['permissionDecision'], 'deny', command)
                self.assertNotIn('updatedInput', denied, command)
            # Recorded: 76 plain commands that only needed approval (git commit, ssh) denied as unwrapped
            # in 5 days, each resent wrapped by hand: now wrapped and asked for, naming the policy.
            for command in ('git commit -q -m x', "cd /tmp && git stash", 'ssh host hostname'):
                asked = output(command)
                self.assertEqual(asked['permissionDecision'], 'ask', command)
                self.assertIn('Agent policy requires explicit user approval', asked['permissionDecisionReason'])
                self.assertEqual(asked['updatedInput'], {'command': shlex.join(['Ω-run', '--shell', command])})
            # Code Mode code receives the output itself: run as written, policies still apply.
            nested = {'tool_use_id': 'repl_1'}
            self.assertEqual(output('ls -la | head', top=nested)['permissionDecision'], 'allow')
            self.assertNotIn('updatedInput', output('ls -la | head', top=nested))
            self.assertEqual(output('git push', top=nested)['permissionDecision'], 'ask')
            self.assertEqual(output('rm -rf x', top=nested)['permissionDecision'], 'deny')
            self.assertEqual(output("'Ω-run' -- ls | head", top=nested)['updatedInput'],
                             {'command': "'Ω-run' --shell 'ls | head'"})  # a wrapper call is checked as usual
            self.assertEqual(output('ls', args=())['permissionDecision'], 'deny')  # Codex: no rewrite
            self.assertEqual(output('ls', tool='exec_command')['permissionDecision'], 'deny')

    def test_zsh_equals_argument_denied(self):
        self.assertDenied(self.call(shlex.join([NATIVE, 'run', '--', 'echo', '=ls'])))
        self.assertDenied(self.call(f'{NATIVE} run -- rg -n x =='))
        # Recorded 3 times: a quoted "==" is text to zsh, not an EQUALS expansion.
        for command in (f'{NATIVE} inspect /tmp/e --search "=="', f"{NATIVE} run -- rg -n '=x' src"):
            self.assertEqual(self.call(command), {}, command)

    def test_bounded_reports_take_read_only_filters(self):
        # Recorded: `Ω-resume TASK 2>&1 | head -c 20000` denied; the report is bounded like Ω-inspect.
        self.assertEqual(self.call("'Ω-resume' t 2>&1 | head -c 20000"), self.call("'Ω-resume' t"))
        self.assertNotEqual(self.call("'Ω-resume' t | sh"), self.call("'Ω-resume' t"))

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
                      'pkill -f self-match', 'Ω-inspect is already bounded', 'Double quotes keep $, backquote and ! live',
                      'Heredoc: create the file', 'Nothing may precede or follow the wrapper',
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
