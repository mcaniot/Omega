#!/usr/bin/env python3
"""Golden-output tests for the executor CLI, inspect and prune.

OMEGA_RUN_BINARY selects the executor under test (default: the deployed binary).
fixtures/run_golden.json.gz holds the normalized outputs (stdout, stderr, exit
code, result.json, evidence logs, surviving evidence names) recorded from the
original C/Python executor for every case below. Each case runs in a private
HOME; volatile values (evidence names, home, interpreter path, durations, pids,
timestamps) are normalized before comparison.
"""
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

BINARY = os.environ.get('OMEGA_RUN_BINARY', str(Path.home() / '.agents/tools/robot-exec/native/run'))
GOLDEN = Path(__file__).with_name('fixtures') / 'run_golden.json.gz'
PY = sys.executable

VOLATILE = [
    (re.compile(r'\d{8}T\d{6}-(?:native-[A-Za-z0-9]{6}|\w{8})'), '<EV>'),
    (re.compile(r'"duration_seconds": ?[0-9.]+'), '"duration_seconds":<D>'),
    (re.compile(r'"wrapper_pid": ?\d+'), '"wrapper_pid":<P>'),
    (re.compile(r'"pid": ?\d+'), '"pid":<P>'),
    (re.compile(r'"process_group": ?\d+'), '"process_group":<P>'),
    (re.compile(r'"(started_at|finished_at)": ?"[^"]*"'), r'"\1":<T>'),
]


def normalize(data, home):
    text = data.decode('utf-8', 'surrogateescape').replace(home, '<HOME>').replace(PY, '<PY>')
    for pattern, replacement in VOLATILE:
        text = pattern.sub(replacement, text)
    return text


def compact_envelope(text):
    """Derive the compact stdout envelope from a recorded legacy one: no duration, no empty diagnostics,
    and byte counts only when output exists but is not inline."""
    if not text.startswith('{"status":'):
        return text
    text = re.sub(r'"duration_seconds":<D>,', '', text, count=1)
    text = text.replace(',"diagnostics":[]', '')
    match = re.search(r'"bytes":\{"stdout":(\d+),"stderr":(\d+)\},', text)
    if match and (re.search(r'"std(?:out|err)":"', text[match.end():]) or sum(map(int, match.groups())) == 0):
        text = text[:match.start()] + text[match.end():]
    return text


def shell(script):
    # Frozen fixtures describe legacy shell semantics. Strict defaults have
    # dedicated mandatory-step tests in tools/test_run.py.
    return ['run', '--shell-keep-going', '--shell', script]


def py(code, *options):
    return ['run', *options, '--', PY, '-c', code]


# (name, argv builder, native_metadata): native_metadata cases also compare result.json.
CASES = [
    ('true', lambda h: ['run', '--', 'true'], True),
    ('echo', lambda h: ['run', '--', 'echo', 'hello world'], True),
    ('fail_stderr', lambda h: shell('echo oops >&2; exit 3'), True),
    ('shell_exit', lambda h: shell('printf evidence; exit 19'), True),
    ('shell_executable', lambda h: ['run', '--shell-keep-going', '--shell-executable', '/bin/sh', '--shell', 'echo $0'], True),
    ('large_success', lambda h: py('print("x"*3000)'), True),
    ('large_traceback', lambda h: py('import sys\nprint("noise\\n"*2000)\nraise RuntimeError("boom")'), True),
    ('many_errors', lambda h: py('for i in range(40):\n print(f"ValueError: bad {i}")\n print("main.py:%d warning" % i)\nraise SystemExit(1)'), True),
    ('weak_words', lambda h: py('print("failed once\\n" + "ok\\n"*1500)\nraise SystemExit(2)'), True),
    ('passed_lines', lambda h: py('print("test_a PASSED\\n"*400 + "a failure here")\nraise SystemExit(1)'), True),
    ('ansi', lambda h: py('print("\\x1b[31mred\\x1b[0m done")'), True),
    ('unicode', lambda h: py('print("caf\\u00e9 \\u2603 \\U0001f600")'), True),
    ('unicode_failure_large', lambda h: py('print("\\u00e9rror: \\u2603 " * 600)\nraise SystemExit(1)'), True),
    ('invalid_utf8', lambda h: py('import sys\nsys.stdout.buffer.write(b"a\\xffb\\xc3\\n")'), True),
    ('nul_byte', lambda h: py('import sys\nsys.stdout.buffer.write(b"a\\x00b\\n")'), True),
    ('control_bytes', lambda h: py('import sys\nsys.stdout.buffer.write(b"a\\x01\\x08\\x0c\\t\\r\\x7fb\\n")'), True),
    ('long_line', lambda h: py('print("y"*30000)\nraise SystemExit(1)'), True),
    ('json_inflation', lambda h: py('import sys\nsys.stdout.buffer.write(b"\\x01"*900)'), True),
    ('inline_zero', lambda h: py('print("quiet")', '--inline-bytes', '0'), True),
    ('inline_large', lambda h: py('print("z"*500)', '--inline-bytes', '100000'), True),
    ('missing_executable', lambda h: ['run', '--', '/no/such/executable'], True),
    ('not_executable', lambda h: ['run', '--', '/etc/passwd'], True),
    ('missing_cwd', lambda h: ['run', '--cwd', h + '/missing', '--', 'true'], False),
    ('cwd_option', lambda h: ['run', '--cwd', '/tmp', '--', 'pwd'], True),
    ('timeout', lambda h: py('import time; time.sleep(30)', '--timeout', '0.3', '--kill-after', '0.5'), True),
    ('timeout_ignores_term', lambda h: py('import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)', '--timeout', '0.2', '--kill-after', '0.4'), True),
    ('crash_segv', lambda h: py('import os,signal; os.kill(os.getpid(), signal.SIGSEGV)'), True),
    ('crash_abort', lambda h: py('import os; os.abort()'), True),
    ('self_term', lambda h: py('import os,signal; os.kill(os.getpid(), signal.SIGTERM)'), True),
    ('background_child', lambda h: shell('sleep 30 & echo started'), True),
    ('noninteractive_stdin', lambda h: py('import sys; print(repr(sys.stdin.read()))'), True),
    ('sigpipe_default', lambda h: shell('trap -p SIGPIPE; (trap -p) | wc -l'), True),
    ('process_group', lambda h: py('import os; print(os.getpid()==os.getpgrp()==os.getsid(0))'), True),
    ('env_passthrough', lambda h: shell('echo "$HOME"'), True),
    ('double_dash_options', lambda h: ['run', '--', '--version'], True),
    ('dash_command', lambda h: ['run', '--', '-', 'x'], True),
    ('command_without_marker', lambda h: ['run', 'echo', 'plain'], True),
    ('both_shell_and_argv', lambda h: ['run', '--shell', 'true', '--', 'true'], True),
    ('no_command', lambda h: ['run'], True),
    ('no_command_marker', lambda h: ['run', '--'], True),
    ('timeout_zero', lambda h: ['run', '--timeout', '0', '--', 'true'], True),
    ('timeout_negative', lambda h: ['run', '--timeout', '-1', '--', 'true'], True),
    ('kill_after_zero', lambda h: ['run', '--kill-after', '0', '--', 'true'], True),
    ('max_log_zero', lambda h: ['run', '--max-log-bytes', '0', '--', 'true'], True),
    ('bad_number', lambda h: ['run', '--timeout', 'abc', '--', 'true'], True),
    ('bad_integer', lambda h: ['run', '--inline-bytes', '1.5', '--', 'true'], True),
    ('inf_timeout', lambda h: ['run', '--timeout', 'inf', '--', 'true'], True),
    ('hex_timeout', lambda h: ['run', '--timeout', '0x10', '--', 'true'], True),
    ('abbreviation', lambda h: ['run', '--time', '5', '--', 'true'], False),
    ('equals_form', lambda h: ['run', '--timeout=5', '--', 'true'], False),
    ('equals_shell', lambda h: ['run', '--shell=echo hi'], False),
    ('ambiguous_abbreviation', lambda h: ['run', '--s', 'x'], False),
    ('missing_value', lambda h: ['run', '--timeout'], False),
    ('missing_value_marker', lambda h: ['run', '--cwd', '--', 'x'], False),
    ('unknown_option', lambda h: ['run', '--bogus', 'x'], False),
    ('invalid_float', lambda h: ['run', '--timeout', 'abc', '--', 'x'], True),
    ('native_only_ok', lambda h: ['run', '--native-only', '--', 'echo', 'native'], True),
    ('native_only_unsupported', lambda h: ['run', '--native-only', '--time', '5', '--', 'true'], True),
    ('native_only_unicode', lambda h: ['run', '--native-only', '--', 'echo', 'café'], True),
    ('unicode_argument', lambda h: ['run', '--', 'echo', 'café'], False),
    ('no_args', lambda h: [], False),
    ('help', lambda h: ['--help'], False),
    ('help_short', lambda h: ['-h'], False),
    ('run_help', lambda h: ['run', '-h'], False),
    ('inspect_help', lambda h: ['inspect', '-h'], False),
    ('prune_help', lambda h: ['prune', '-h'], False),
    ('bogus_operation', lambda h: ['bogus'], False),
    ('stray_option', lambda h: ['-x', 'run'], False),
    ('inspect_missing', lambda h: ['inspect'], False),
    ('inspect_extra', lambda h: ['inspect', 'a', 'b'], False),
    ('inspect_bad_stream', lambda h: ['inspect', 'x', '--stream', 'foo'], False),
    ('inspect_bad_lines', lambda h: ['inspect', 'x', '--lines', '1.5'], False),
    ('prune_extra', lambda h: ['prune', 'extra'], False),
    ('prune_bad_days', lambda h: ['prune', '--days', 'x'], False),
    ('prune_flag_value', lambda h: ['prune', '--apply=1'], False),
]


def lines_file(home):
    """Evidence directory with a deterministic multi-line stdout.log for inspect cases."""
    directory = Path(home) / 'fixture'
    directory.mkdir()
    body = ''.join(f'line {i} {"fatal marker" if i % 7 == 0 else "plain"} é\n' for i in range(300))
    (directory / 'stdout.log').write_text(body)
    (directory / 'stderr.log').write_bytes(b'abc\xff\ndef fatal boundary marker ' + b'q' * 3000 + b'\nlast')
    return str(directory)


INSPECT = [
    ('inspect_default', lambda h: ['inspect', lines_file(h)]),
    ('inspect_stdout_all', lambda h: ['inspect', lines_file(h), '--stream', 'stdout', '--lines', '40', '--max-bytes', '7000']),
    ('inspect_offset', lambda h: ['inspect', lines_file(h), '--stream', 'stdout', '--offset', '100', '--lines', '3']),
    ('inspect_offset_past_end', lambda h: ['inspect', lines_file(h), '--stream', 'stdout', '--offset', '999999']),
    ('inspect_search', lambda h: ['inspect', lines_file(h), '--stream', 'stdout', '--search', 'fatal marker']),
    ('inspect_search_stderr', lambda h: ['inspect', lines_file(h), '--search', 'boundary marker']),
    ('inspect_search_unicode', lambda h: ['inspect', lines_file(h), '--stream', 'stdout', '--search', 'é']),
    ('inspect_search_single_byte', lambda h: ['inspect', lines_file(h), '--search', 'q', '--lines', '5']),
    ('inspect_search_missing', lambda h: ['inspect', lines_file(h), '--search', 'absent']),
    ('inspect_small_budget', lambda h: ['inspect', lines_file(h), '--stream', 'stdout', '--max-bytes', '50']),
    ('inspect_invalid_utf8', lambda h: ['inspect', lines_file(h), '--lines', '1']),
    ('inspect_lines_too_many', lambda h: ['inspect', lines_file(h), '--lines', '100']),
    ('inspect_bytes_too_many', lambda h: ['inspect', lines_file(h), '--max-bytes', '8000']),
    ('inspect_negative_offset', lambda h: ['inspect', lines_file(h), '--offset', '-1']),
    ('inspect_search_long', lambda h: ['inspect', lines_file(h), '--search', 'x' * 513]),
    ('inspect_missing_file', lambda h: ['inspect', h + '/nowhere']),
    ('inspect_relative', lambda h: ['inspect', 'fixture/../fixture']),
]


def evidence_fixture(home):
    root = Path(home) / '.agents/tasks'
    (root / 'active').mkdir(parents=True, exist_ok=True)
    (root / 'evidence').mkdir(parents=True, exist_ok=True)
    for name, age in (('20200101T000000-native-AAAAAA', 40), ('20200102T000000-native-BBBBBB', 40),
                      ('20200103T000000-abcdefgh', 40), ('20990101T000000-native-CCCCCC', 0), ('not-evidence', 40)):
        directory = root / 'evidence' / name
        directory.mkdir(exist_ok=True)
        (directory / 'stdout.log').write_text('x' * 10)
        (directory / 'stderr.log').write_text('y' * 5)
        stamp = Path(home).stat().st_mtime - age * 86400
        os.utime(directory, (stamp, stamp))
    (root / 'active' / 'task.md').write_text('keeps 20200102T000000-native-BBBBBB and 20200199T000000-native-ZZZZZZ\n')
    (root / 'evidence' / 'link').symlink_to(root / 'evidence' / '20200101T000000-native-AAAAAA')
    return []


PRUNE = [
    ('prune_dry_run', lambda h: ['prune', '--days', '7'] + evidence_fixture(h)),
    ('prune_default_days', lambda h: ['prune'] + evidence_fixture(h)),
    ('prune_apply', lambda h: ['prune', '--days', '7', '--apply'] + evidence_fixture(h)),
    ('prune_nonpositive', lambda h: ['prune', '--days', '0'] + evidence_fixture(h)),
    ('prune_no_root', lambda h: ['prune']),
]


SHORT_ESCAPES = {'0008': 'b', '0009': 't', '000a': 'n', '000c': 'f', '000d': 'r'}
LONG_ESCAPE = re.compile(r'(?<!\\)((?:\\\\)*)\\u(0008|0009|000a|000c|000d)')


def short_escapes(value):
    """Map JSON `\\u000a`-style escapes to `\\n`: the recorded C encoder used the long
    form, the executor now emits the equivalent short one to save model context."""
    return LONG_ESCAPE.sub(lambda m: m.group(1) + '\\' + SHORT_ESCAPES[m.group(2)], value)


def comparable(observed):
    """Observed results with JSON escapes in stdout and result.json in short form."""
    observed = dict(observed)
    observed['stdout'] = short_escapes(observed['stdout'])
    if 'metadata' in observed:
        observed['metadata'] = [short_escapes(item) for item in observed['metadata']]
    return observed


def execute(binary, builder):
    """Run one case in a private HOME and return its normalized observable results."""
    with tempfile.TemporaryDirectory() as home:
        home = os.path.realpath(home)
        Path(home, ".agents/tasks").mkdir(parents=True)
        argv = builder(home)
        process = subprocess.run([binary, *argv], capture_output=True, stdin=subprocess.DEVNULL, cwd=home,
                                 env={**os.environ, "HOME": home}, timeout=60)
        evidence = Path(home, ".agents/tasks/evidence")
        runs = sorted(evidence.glob("2*-*")) if evidence.exists() else []
        metadata, logs = [], []
        for directory in runs:
            if (directory / "result.json").exists() and (directory / "stdout.log").exists():
                metadata.append(normalize((directory / "result.json").read_bytes(), home))
                logs.append([normalize((directory / "stdout.log").read_bytes(), home),
                             normalize((directory / "stderr.log").read_bytes(), home)])
        survivors = sorted(path.name for path in evidence.iterdir()) if evidence.exists() else []
        return {
            "code": process.returncode,
            "stdout": normalize(process.stdout, home),
            "stderr": normalize(process.stderr, home),
            "metadata": metadata,
            "logs": logs,
            "survivors": [normalize(name.encode(), home) for name in survivors],
        }


def all_cases():
    """(name, builder, compares result.json) for every golden case."""
    return [*CASES, *[(n, b, False) for n, b in INSPECT], *[(n, b, False) for n, b in PRUNE]]


class GoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.access(BINARY, os.X_OK):
            raise unittest.SkipTest(f"missing executable {BINARY}")
        with gzip.open(GOLDEN, "rt", encoding="utf-8") as handle:
            cls.golden = json.load(handle)

    def test_cases_match_recorded_outputs(self):
        self.assertEqual(sorted(self.golden), sorted(name for name, _, _ in all_cases()))
        for name, builder, native_metadata in all_cases():
            with self.subTest(name):
                actual = execute(BINARY, builder)
                # Additive previews are tested separately; frozen status, diagnostics,
                # metadata and complete logs must remain compatible.
                actual['stdout'] = re.sub(r',"preview":\[.*\](?=}\n?$)', '', actual['stdout'], flags=re.S)
                if not native_metadata:
                    actual.pop("metadata")
                actual = comparable(actual)
                expected = comparable(self.golden[name])
                expected['stdout'] = compact_envelope(expected['stdout'])
                if name == 'run_help':
                    expected['stdout'] = expected['stdout'].replace(
                        '  --shell SHELL\n',
                        '  --validation\n'
                        '                        Require an independent argv check (reject --shell)\n'
                        '  --shell SHELL\n')
                    expected['stdout'] = expected['stdout'].replace(
                        '  --shell SHELL\n',
                        '  --shell SHELL\n'
                        '                        Fail fast with errexit and pipefail (bash/zsh)\n'
                        '  --shell-keep-going\n'
                        '                        Explicit legacy shell semantics; not for validations\n')
                if name == 'ambiguous_abbreviation':
                    expected['stderr'] = expected['stderr'].replace(
                        'could match --shell, --shell-executable',
                        'could match --shell, --shell-keep-going, --shell-executable')
                # These recorded outputs exceeded the old inline/JSON budgets. Derive
                # only their new summary from the frozen logs; retain all other evidence.
                if name in {'large_success', 'weak_words', 'passed_lines',
                            'unicode_failure_large', 'json_inflation'}:
                    actual = dict(actual)
                    actual_summary = json.loads(actual.pop('stdout'))
                    expected = dict(expected)
                    expected_summary = json.loads(expected.pop('stdout'))
                    out, err = expected['logs'][0]
                    if out:
                        expected_summary['stdout'] = out
                    if err:
                        expected_summary['stderr'] = err
                    expected_summary.pop('diagnostics', None)
                    if out or err:
                        expected_summary.pop('bytes', None)
                    self.assertEqual(actual_summary, expected_summary)
                self.assertEqual(actual, expected)

    def test_log_limit(self):
        # Bytes written before the kill lands are timing dependent, so compare status and exit code only.
        builder = lambda h: py("import sys\nwhile True: sys.stdout.write(\"x\"*1000)", "--max-log-bytes", "50000", "--kill-after", "0.5")
        result = execute(BINARY, builder)
        self.assertEqual(result["code"], 125)
        self.assertIn("\"status\":\"LOG_LIMIT\"", result["stdout"])

    def test_result_json_is_valid(self):
        result = execute(BINARY, lambda h: ["run", "--", "true"])
        text = result["metadata"][0].replace("<D>", "0").replace("<P>", "0").replace("<T>", "\"\"").replace("<EV>", "x")
        self.assertEqual(json.loads(text)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
