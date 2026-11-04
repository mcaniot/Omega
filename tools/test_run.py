#!/usr/bin/env python3
"""Behavioral tests for captured execution; generated evidence is retained."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time
import unittest

# OMEGA_RUN_BINARY selects the executor under test (default: the deployed binary).
RUNNER = [os.environ.get("OMEGA_RUN_BINARY", str(Path.home() / ".agents/tools/robot-exec/native/run"))]


def _readable(path):
    try:
        path.read_bytes()
        return True
    except OSError:
        return False


def invoke(code, *options, shell=False):
    command = [sys.executable, '-c', code]
    arguments = ['--shell', shlex.join(command)] if shell else ['--', *command]
    process = subprocess.run([*RUNNER, 'run', *options, *arguments], capture_output=True)
    assert not process.stderr, process.stderr
    assert len(process.stdout) <= 12 * 1024 + 1, len(process.stdout)
    result = json.loads(process.stdout)
    if 'stdout' not in result and 'stderr' not in result:
        assert len(process.stdout) <= 4001, len(process.stdout)
    return process.returncode, result, Path(result['evidence'])


class CaptureTests(unittest.TestCase):
    def test_smart_preview_preserves_edges_errors_and_complete_logs(self):
        for shell in (False, True):
            with self.subTest(shell=shell):
                code = ("import sys;print('preview start');print('noise\\n'*3000);"
                        "print('ERROR: middle diagnostic');print('more noise\\n'*3000);"
                        "print('preview end');sys.stderr.write('stderr start\\n'+'warning\\n'*3000+'stderr end\\n')")
                status, result, path = invoke(code, shell=shell)
                self.assertEqual(status, 0)
                preview = json.dumps(result['preview'])
                for marker in ('preview start', 'preview end', 'ERROR: middle diagnostic',
                               'stderr start', 'stderr end'):
                    self.assertIn(marker, preview)
                self.assertGreater((path / 'stdout.log').stat().st_size, 50000)
                self.assertIn('ERROR: middle diagnostic', (path / 'stdout.log').read_text())

    def test_preview_long_line_tail_and_explicit_silence(self):
        _, result, _ = invoke("print('START'+ 'x'*30000 + 'END')")
        self.assertIn('START', json.dumps(result['preview']))
        self.assertIn('END', json.dumps(result['preview']))
        _, result, _ = invoke("print('x'*30000)", '--inline-bytes', '0')
        self.assertEqual(result['diagnostics'], [])

    def test_shell_mandatory_steps_fail_fast(self):
        for script in ('false; echo masked', 'false | cat; echo masked',
                       'false; echo masqué',
                       'x=$(false; echo masked); echo masked', '(false; echo masked); echo masked',
                       'check() { false; echo masked; }; check; echo masked'):
            with self.subTest(script=script):
                process = subprocess.run([*RUNNER, 'run', '--shell', script], capture_output=True)
                result = json.loads(process.stdout)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(result['status'], 'FAIL')
                self.assertNotIn('masked', (Path(result['evidence']) / 'stdout.log').read_text())

    def test_zsh_fail_fast_when_available(self):
        if not Path('/bin/zsh').exists():
            self.skipTest('zsh unavailable')
        process = subprocess.run([*RUNNER, 'run', '--shell-executable', '/bin/zsh',
                                  '--shell', 'false | cat; echo masked'], capture_output=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)['status'], 'FAIL')

    def test_shell_explicit_recovery_and_legacy_compatibility(self):
        for script, options in [('false || echo recovered', []),
                                ('if false; then exit 9; fi; echo recovered', []),
                                ('false; echo recovered', ['--shell-keep-going']),
                                ('false; echo récupéré', ['--shell-keep-going'])]:
            process = subprocess.run([*RUNNER, 'run', *options, '--shell', script], capture_output=True)
            self.assertEqual(process.returncode, 0, process.stdout)
        for options in (['--shell-keep-going'], ['--shell-keep-going=true']):
            process = subprocess.run([*RUNNER, 'run', *options, '--', 'true'], capture_output=True)
            self.assertNotEqual(process.returncode, 0)

    def test_success_flood(self):
        status, result, path = invoke("import sys;sys.stdout.write('noise\\n'*100000);sys.stderr.write('warning\\n'*50000)")
        self.assertEqual(status, 0)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual((path/'stdout.log').stat().st_size, 600000)
        self.assertEqual((path/'stderr.log').stat().st_size, 400000)
        metadata = json.loads((path/'result.json').read_text())
        self.assertEqual(metadata['cwd'], os.getcwd())
        self.assertEqual(metadata['command'][-1], "import sys;sys.stdout.write('noise\\n'*100000);sys.stderr.write('warning\\n'*50000)")
        self.assertGreater(metadata['duration_seconds'], 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_failure_traceback_exit(self):
        status, result, path = invoke("print('noise'*100000);raise AssertionError('controller.cpp:42 unsafe limit')")
        self.assertEqual(status, 1)
        self.assertEqual(result['exit_code'], 1)
        self.assertIn('Traceback', (path/'stderr.log').read_text())
        self.assertIn('unsafe limit', (path/'stderr.log').read_text())
        self.assertIn('controller.cpp:42', json.dumps(result['diagnostics']))
        _, result, _ = invoke("print('t.py::test_reports_failure PASSED\\n'*100+'E   real error here\\n'+'x'*3000);raise SystemExit(1)",
                              '--inline-bytes', '0')
        self.assertIn('real error here', json.dumps(result['diagnostics']))
        self.assertNotIn('PASSED', json.dumps(result['diagnostics']))

    def test_root_cause_not_evicted_by_metric_noise(self):
        # Regression for evidence 20261103T212024-x21a6evf: stdout error drowned by "tracking_error=" lines.
        code = ("print('RuntimeError: QP infeasible at tick 42  (/fake/ctrl/qp_solver.py:88)')\n"
                "for i in range(100000): print(f'[INFO] tick {i} tracking_error={i*1e-6:.6f}')\n"
                "raise SystemExit(1)")
        _, result, _ = invoke(code)
        text = json.dumps(result['diagnostics'])
        self.assertIn('QP infeasible', text)
        self.assertEqual(result['diagnostics'][0]['line'], 1)
        self.assertNotIn('tracking_error', text)

    def test_error_outranks_location_and_warning_noise(self):
        # Council repros: glog "file.cpp:N]" prefixes and Python warnings must not evict real errors.
        cases = {
            "RuntimeError: QP infeasible": "for i in range(20): print(f'I1103 ctrl.cpp:42] tick {i}')",
            "[Error] [omni.physx.plugin] PhysX error": "for i in range(20): print(f'/x/a.py:{i}: DeprecationWarning: old')",
            "terminate called after throwing an instance of 'std::runtime_error'": "for i in range(20): print(f'I1103 ctrl.cpp:{i}] tick')",
        }
        for error, noise in cases.items():
            with self.subTest(error=error):
                code = f"{noise}\nprint({error!r})\n{noise}\nraise SystemExit(1)"
                _, result, _ = invoke(code, '--inline-bytes', '0')
                self.assertIn(error, [item['text'] for item in result['diagnostics']])

    def test_first_and_last_strong_matches_kept(self):
        code = ("print('ValueError: first cause')\n"
                "for i in range(50): print(f'KeyError: repeat {i}')\n"
                "print('weak error word only')\n"
                "raise SystemExit(2)")
        _, result, _ = invoke(code, '--inline-bytes', '0')
        texts = [item['text'] for item in result['diagnostics']]
        self.assertEqual(texts[0], 'ValueError: first cause')
        self.assertIn('KeyError: repeat 49', texts)
        self.assertLessEqual(len(texts), 8)
        self.assertNotIn('weak error word only', texts)

    def test_weak_word_fallback(self):
        _, result, _ = invoke("print('loss_error=0.1\\n'*1000+'build failed for target x\\n'+'done\\n'*10);raise SystemExit(1)")
        texts = [item['text'] for item in result['diagnostics']]
        self.assertEqual(texts, ['build failed for target x'])

    def test_nonzero_and_shell(self):
        status, _, _ = invoke('raise SystemExit(37)')
        self.assertEqual(status, 37)
        process = subprocess.run([*RUNNER, 'run', '--shell', 'printf evidence; exit 19'], capture_output=True)
        self.assertEqual(process.returncode, 19)
        result = json.loads(process.stdout)
        self.assertEqual((Path(result['evidence'])/'stdout.log').read_text(), 'evidence')

    def test_timeout_and_ignored_term(self):
        start = time.monotonic()
        status, result, path = invoke('import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("started",flush=True);time.sleep(30)', '--timeout', '0.15', '--kill-after', '1')
        self.assertEqual(status, 124)
        self.assertEqual(result['status'], 'TIMEOUT')
        self.assertLess(time.monotonic()-start, 4)
        self.assertEqual((path/'stdout.log').read_text(), 'started\n')

    def test_interrupt(self):
        process = subprocess.Popen([*RUNNER, 'run', '--', sys.executable, '-c', 'import time;print("started",flush=True);time.sleep(30)'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.3)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=5)
        self.assertFalse(err)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(json.loads(out)['status'], 'INTERRUPTED')

    def test_child_signal_is_a_crash_not_an_interrupt(self):
        status, result, _ = invoke('import os,signal;os.kill(os.getpid(),signal.SIGTERM)')
        self.assertEqual(status, 143)
        self.assertEqual((result['status'], result['signal']), ('CRASHED', 'SIGTERM'))
        status, result, path = invoke("import os,signal,sys;print('x'*3000);sys.stdout.flush();os.kill(os.getpid(),signal.SIGSEGV)")
        self.assertEqual(status, 139)
        self.assertEqual((result['status'], result['signal']), ('CRASHED', 'SIGSEGV'))
        self.assertEqual(json.loads((path/'result.json').read_text())['signal'], 'SIGSEGV')

    def test_wrapper_sigkill_does_not_orphan_child(self):
        marker = f'orphan-probe-{os.getpid()}-{time.monotonic_ns()}'
        process = subprocess.Popen([*RUNNER, 'run', '--kill-after', '0.5', '--', sys.executable, '-c',
                                    f'import time;time.sleep(30) # {marker}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        process.kill()
        process.communicate(timeout=5)
        deadline = time.monotonic() + 4
        alive = True
        while alive and time.monotonic() < deadline:
            alive = any(marker in path.read_text(errors='replace') for path in Path('/proc').glob('[0-9]*/cmdline') if _readable(path))
            time.sleep(0.1)
        self.assertFalse(alive)

    def test_log_limit_stops_runaway_output(self):
        status, result, path = invoke("import sys\nwhile True: sys.stdout.write('x'*65536)", '--max-log-bytes', '1000000', '--kill-after', '1')
        self.assertEqual((status, result['status']), (125, 'LOG_LIMIT'))
        # Overshoot is bounded by one 25 ms poll of write throughput, not by the run length.
        self.assertLess((path/'stdout.log').stat().st_size, 500_000_000)

    def test_evidence_is_private(self):
        _, _, path = invoke('print(1)')
        self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
        for name in ('stdout.log', 'stderr.log', 'result.json'):
            self.assertEqual((path/name).stat().st_mode & 0o077, 0, name)

    def test_prune_defaults_to_dry_run(self):
        process = subprocess.run([*RUNNER, 'prune', '--days', '100000'], capture_output=True)
        result = json.loads(process.stdout)
        self.assertEqual((process.returncode, result['applied'], result['runs']), (0, False, 0))

    def test_concurrent_unique_paths(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(invoke, ['print(1)'] * 4))
        self.assertEqual(len({str(result[2]) for result in outcomes}), 4)
        self.assertTrue(all(result[0] == 0 for result in outcomes))

    def test_long_line_bounded_inspection_and_pagination(self):
        _, _, path = invoke("import sys;sys.stdout.buffer.write(b'\\x00'*200000+b'END\\n')")
        args = [*RUNNER, 'inspect', str(path), '--stream', 'stdout', '--lines', '40', '--max-bytes', '7000']
        process = subprocess.run(args, capture_output=True)
        self.assertLess(len(process.stdout), 8192)
        result = json.loads(process.stdout)
        self.assertGreater(result['next_offset'], 0)
        self.assertLess(result['next_offset'], 200004)
        self.assertFalse(result['eof'])
        process2 = subprocess.run(args + ['--offset', str(result['next_offset'])], capture_output=True)
        result2 = json.loads(process2.stdout)
        self.assertEqual(result2['fragments'][0]['offset'], result['next_offset'])
        self.assertEqual((path/'stdout.log').stat().st_size, 200004)

    def test_inspect_search_and_bounds(self):
        _, _, path = invoke("print('routine\\n'*10000+'fatal marker\\nmore')")
        process = subprocess.run([*RUNNER, 'inspect', str(path), '--stream', 'stdout', '--search', 'fatal marker'], capture_output=True)
        result = json.loads(process.stdout)
        self.assertEqual(len(result['fragments']), 1)
        self.assertIn('fatal marker', result['fragments'][0]['text'])
        invalid = subprocess.run([*RUNNER, 'inspect', str(path), '--lines', '100'], capture_output=True)
        self.assertEqual(invalid.returncode, 2)

    def test_search_across_chunk_boundary(self):
        _, _, path = invoke("print('x'*1020+'fatal boundary marker'+'y'*5000)")
        process = subprocess.run([*RUNNER, 'inspect', str(path), '--stream', 'stdout', '--search', 'fatal boundary marker'], capture_output=True)
        result = json.loads(process.stdout)
        self.assertEqual(len(result['fragments']), 1)
        self.assertIn('fatal boundary marker', result['fragments'][0]['text'])

    def test_small_output_inlined_large_not(self):
        status, result, path = invoke("import sys;print('short');sys.stderr.write('warn\\n')")
        self.assertEqual(status, 0)
        self.assertEqual((result['stdout'], result['stderr']), ('short\n', 'warn\n'))
        self.assertEqual((path/'stdout.log').read_text(), 'short\n')
        _, result, _ = invoke("print('x'*8192)")
        self.assertNotIn('stdout', result)
        _, result, _ = invoke("print('short')", '--inline-bytes', '0')
        self.assertNotIn('stdout', result)
        _, result, _ = invoke("import sys;sys.stdout.buffer.write(b'\\x01'*8192)")
        self.assertNotIn('stdout', result)

    def test_inline_thresholds_and_overrides(self):
        # --shell forces the full parser/encoder path; argv exercises the native fast path.
        for shell in (False, True):
            for size in (8191, 8192, 8193):
                with self.subTest(shell=shell, size=size):
                    out = 'x' * (size // 2)
                    err = 'y' * (size - len(out))
                    status, result, path = invoke(
                        f'import sys;sys.stdout.write({out!r});sys.stderr.write({err!r})', shell=shell)
                    self.assertEqual(status, 0)
                    self.assertEqual((path/'stdout.log').read_text(), out)
                    self.assertEqual((path/'stderr.log').read_text(), err)
                    if size <= 8192:
                        self.assertEqual((result['stdout'], result['stderr']), (out, err))
                    else:
                        self.assertNotIn('stdout', result)
                        self.assertNotIn('stderr', result)
            for limit, size, included in ((0, 1, False), (32, 32, True), (32, 33, False),
                                          (9000, 8500, True)):
                with self.subTest(shell=shell, limit=limit, size=size):
                    _, result, _ = invoke(f'import sys;sys.stdout.write("x"*{size})',
                                          '--inline-bytes', str(limit), shell=shell)
                    self.assertEqual('stdout' in result, included)

    def test_escaped_inline_budget(self):
        for shell in (False, True):
            with self.subTest(shell=shell):
                _, result, path = invoke("import sys;sys.stdout.buffer.write(b'\\x01'*8192)", shell=shell)
                self.assertNotIn('stdout', result)
                self.assertEqual((path/'stdout.log').read_bytes(), b'\x01' * 8192)

    def test_missing_executable(self):
        process = subprocess.run([*RUNNER, 'run', '--', '/no/such/executable'], capture_output=True)
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 127)
        self.assertIn('FileNotFoundError', (Path(result['evidence'])/'stderr.log').read_text())

    def test_background_children_cleanup(self):
        start = time.monotonic()
        status, _, path = invoke('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);print(p.pid)')
        self.assertEqual(status, 0)
        self.assertLess(time.monotonic()-start, 4)
        pid = int((path/'stdout.log').read_text())
        statusfile = Path(f'/proc/{pid}/status')
        if statusfile.exists():
            self.assertIn('Z (zombie)', statusfile.read_text())
        self.assertTrue(json.loads((path/'result.json').read_text())['background_children_terminated'])


if __name__ == '__main__':
    unittest.main()
