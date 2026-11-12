#!/usr/bin/env python3
"""Local, synthetic before/after replay. No model calls, installs or network access.

Optional token measurement uses an already installed tokenizers and a local JSON
tokenizer. These are tokenizer counts, not provider billing/context telemetry.
"""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import tempfile
import time


def replay(run, optimize, home, count_tokens):
    env = dict(os.environ, HOME=str(home))
    (home / '.agents/tasks').mkdir(parents=True)
    calls = []

    def command(argv, expected=0, read=False):
        start = time.perf_counter()
        done = subprocess.run([str(a) for a in argv], env=env, capture_output=True,
                              text=True, timeout=30)
        elapsed = time.perf_counter() - start
        if done.returncode != expected:
            raise RuntimeError(f'{argv}: exit {done.returncode}, expected {expected}: {done.stderr} {done.stdout[:4000]}')
        calls.append({'latency_ms': elapsed * 1000, 'read': read,
                      'returned_tokens': count_tokens(done.stdout),
                      'command_tokens': count_tokens(shlex.join([str(a) for a in argv]))})
        return json.loads(done.stdout)

    def metrics(begin):
        sample = calls[begin:]
        return {'calls': len(sample), 'reads': sum(c['read'] for c in sample),
                'latency_ms': sum(c['latency_ms'] for c in sample),
                'returned_tokens': (sum(c['returned_tokens'] for c in sample)
                                    if sample[0]['returned_tokens'] is not None else None),
                'command_tokens': (sum(c['command_tokens'] for c in sample)
                                   if sample[0]['command_tokens'] is not None else None)}

    markers = ['PREVIEW_BEGIN', 'ERROR: benchmark middle', 'PREVIEW_END']
    text = markers[0] + '\n' + 'stable filler line for bounded preview\n' * 1000
    text += markers[1] + '\n' + 'second filler line\n' * 1000 + markers[2] + '\n'
    source = home / 'source.txt'
    source.write_text(text)
    begin = len(calls)
    result = command([run, 'run', '--', 'cat', source], read=True)
    log = Path(result['evidence']) / 'stdout.log'
    if log.read_bytes() != source.read_bytes():
        raise RuntimeError('Complete evidence was not preserved')
    visible = json.dumps(result)
    for marker in markers:
        if marker not in visible:
            expanded = command([run, 'inspect', result['evidence'], '--stream', 'stdout',
                                '--search', marker, '--lines', '1'], read=True)
            visible += json.dumps(expanded)
    if not all(marker in visible for marker in markers):
        raise RuntimeError('Required preview facts missing')
    preview = metrics(begin)
    preview['log_sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()

    begin = len(calls)
    command([run, 'run', '--', 'printf', 'small output\n'], read=True)
    small = metrics(begin)

    task = home / '.agents/tasks/completed/benchmark.md'
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text('Objective: benchmark resume.\nCurrent: complete.\nEvidence: retained locally.\nNext: none.\n')
    transcript = home / 'historical.jsonl'
    transcript.write_text('historical irrelevant record\n' * 4000 + task.read_text())
    begin = len(calls)
    # Explicit policy replay, not a claim about autonomous agent behavior.
    if optimize.name == 'before-optimize':
        old = command([run, 'run', '--', 'cat', transcript], read=True)
        command([run, 'inspect', old['evidence'], '--stream', 'stdout',
                 '--search', 'Current:', '--lines', '1'], read=True)
        command([run, 'run', '--', 'cat', task], read=True)
    else:
        resumed = command([optimize, 'resume', 'benchmark'], read=True)
        if resumed['content'] != task.read_text() or resumed['truncated']:
            raise RuntimeError('Resume task content changed')
    resume = metrics(begin)

    begin = len(calls)
    strict = optimize.name != 'before-optimize'
    result = command([run, 'run', '--shell', 'printf stage1; false; printf masked'],
                     expected=1 if strict else 0)
    validation = metrics(begin)
    validation['masked_failure'] = result['exit_code'] == 0

    path = home / '.claude/projects/benchmark/session.jsonl'
    path.parent.mkdir(parents=True)
    rows = []
    a = 'Shared source line with enough characters for a content fingerprint.'
    b = 'Modified source line which must remain visible as novel returned data.'
    for i, (cmd, content) in enumerate([('cat source', a), ('sed -n "1p" source', a),
                                        ('head source', a + '\n' + b)]):
        rows.extend([
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
             'id': str(i), 'name': 'Bash', 'input': {'command': cmd}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
             'tool_use_id': str(i), 'content': content}]}}])
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    before = path.read_bytes()
    begin = len(calls)
    audited = command([optimize, '--check', '--json'])
    if path.read_bytes() != before:
        raise RuntimeError('Audit modified the source transcript')
    audit = metrics(begin)
    row, = audited['sessions']
    audit.update({key: row.get(key) for key in
                  ('read_calls', 'overlapping_read_calls', 'repeated_read_lines', 'novel_read_lines')})
    audit['source_reads_issued'] = 3  # advisory auditing never suppresses a read
    return {'preview': preview, 'small_output': small, 'resume_policy': resume,
            'validation': validation, 'read_audit': audit}


ROOT = Path(__file__).resolve().parents[1]
# Baseline of docs/context-reliability.md: the tools before previews, task-first resume and fail-fast shell.
BASELINE_REF = '4c612c4a794058619df6d2dec38a75573a73091e'
# GPT-2 tokenizer used for the published numbers, pinned by hash.
TOKENIZER_URL = 'https://huggingface.co/openai-community/gpt2/resolve/main/tokenizer.json'
TOKENIZER_SHA256 = '8414cab924d8b9b33013f0d221c5862f365ee9be39c5c2bfae8a5a9e970478a6'
TOKENIZER_CACHE = Path.home() / '.cache/omega/gpt2-tokenizer.json'
WORKLOADS = {'preview': 'Oversized read; recover beginning, middle error, end',
             'small_output': 'Small inline output',
             'resume_policy': 'Transcript-first vs task-first resume',
             'validation': 'Mandatory shell failure',
             'read_audit': 'Advisory read audit'}


def build(ref, dest):
    """Build run and optimize from a committed ref in a throwaway tree; Git state is untouched."""
    dest.mkdir()
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', ref, 'rust'], capture_output=True, check=True)
    subprocess.run(['tar', '-x', '-C', str(dest)], input=archive.stdout, check=True)
    subprocess.run(['cargo', 'build', '--quiet', '--release', '--locked', '--manifest-path',
                    str(dest / 'rust/Cargo.toml')], check=True)
    return dest / 'rust/target/release'


NATIVE_READ_LINES = 2000      # Claude Code Read default when no limit is given
NATIVE_BASH_CHARS = 30000     # Claude Code default BASH_MAX_OUTPUT_LENGTH
GUARD_WORKLOADS = {
    'large_read': 'Read a 3000-line file without a limit',
    'reread_same': 'Read the same small file twice',
    'reread_overlap': 'Read lines 1-200, then 101-300',
    'reread_after_edit': 'Read, edit, read again (must not be refused)',
    'build_log': '4000-line build log with one failure, then find it',
    'evidence_read': 'Read a large saved log to find the failure',
}


def native_vs_omega(run, guard, home, count_tokens):
    """Tokens the model receives with plain Claude Code tools vs with the omega guards and Ω-run.

    Plain tools are modelled from their documented defaults (Read: up to 2000 numbered lines;
    Bash: output capped at 30000 characters); omega results come from the real binaries.
    """
    env = dict(os.environ, HOME=str(home))
    (home / '.agents/tasks').mkdir(parents=True, exist_ok=True)
    session = {'n': 0}

    def numbered(path, offset=1, limit=NATIVE_READ_LINES):
        lines = path.read_text().splitlines()[offset - 1:offset - 1 + limit]
        return ''.join(f'{offset + i:6}\t{line}\n' for i, line in enumerate(lines))

    def hook(event, tool, tool_input):
        payload = {'hook_event_name': event, 'tool_name': tool, 'session_id': session['id'],
                   'tool_input': tool_input}
        done = subprocess.run([str(guard)], input=json.dumps(payload), env=env,
                              capture_output=True, text=True, check=True)
        return json.loads(done.stdout).get('hookSpecificOutput', {})

    def omega_read(path, offset=None, limit=None):
        """What the model receives for one Read through context_guard."""
        tool_input = {'file_path': str(path)}
        if offset:
            tool_input['offset'] = offset
        if limit:
            tool_input['limit'] = limit
        out = hook('PreToolUse', 'Read', tool_input)
        if out.get('permissionDecision') == 'deny':
            return out['permissionDecisionReason'], False
        effective = out.get('updatedInput', tool_input)
        text = numbered(path, effective.get('offset', 1), effective.get('limit', NATIVE_READ_LINES))
        return text + out.get('additionalContext', ''), True

    def new_session():
        session['n'] += 1
        session['id'] = f'bench-{session["n"]}'

    def result(native, omega, **extra):
        return {'native_tokens': count_tokens(''.join(native)), 'omega_tokens': count_tokens(''.join(omega)),
                'native_chars': len(''.join(native)), 'omega_chars': len(''.join(omega)), **extra}

    source = home / 'module.py'
    source.write_text(''.join(f'def function_{i}(value):  # helper number {i} with some context\n'
                              for i in range(3000)))
    small = home / 'config.py'
    small.write_text(''.join(f'SETTING_{i} = {i}  # small configuration value\n' for i in range(150)))
    out = {}

    new_session()
    text, _ = omega_read(source)
    if 'bounded this Read to lines 1-200 of 3000' not in text:
        raise RuntimeError('large Read was not bounded')
    out['large_read'] = result([numbered(source)], [text])

    new_session()
    first, _ = omega_read(small)
    second, allowed = omega_read(small)
    if allowed or 'already in context' not in second:
        raise RuntimeError('unchanged re-read was not refused')
    out['reread_same'] = result([numbered(small)] * 2, [first, second])

    new_session()
    first, _ = omega_read(source, 1, 200)
    second, allowed = omega_read(source, 101, 200)
    if allowed or 'offset=201 limit=100' not in second:
        raise RuntimeError('overlapping re-read was not narrowed')
    third, allowed = omega_read(source, 201, 100)
    if not allowed:
        raise RuntimeError('narrowed re-read was refused')
    out['reread_overlap'] = result([numbered(source, 1, 200), numbered(source, 101, 200)],
                                   [first, second, third])

    new_session()
    first, _ = omega_read(small)
    small.write_text(small.read_text() + 'SETTING_NEW = 1\n')
    hook('PostToolUse', 'Edit', {'file_path': str(small)})
    second, allowed = omega_read(small)
    if not allowed or 'SETTING_NEW' not in second:
        raise RuntimeError('re-read after an edit was refused')
    out['reread_after_edit'] = result([numbered(small, limit=150), numbered(small)], [first, second])

    log = home / 'build.log'
    lines = [f'[{i:04}/4000] Compiling crate_{i} v0.{i % 10}.0 (/work/crates/crate_{i})' for i in range(4000)]
    lines[2100] = 'error[E0308]: mismatched types in crates/crate_2100/src/lib.rs:42:17'
    log.write_text('\n'.join(lines) + '\n')
    native_out = log.read_text()
    native_out = native_out[:NATIVE_BASH_CHARS]
    ran = subprocess.run([str(run), 'run', '--', 'cat', str(log)], env=env, capture_output=True, text=True, check=True)
    found = subprocess.run([str(run), 'inspect', json.loads(ran.stdout)['evidence'], '--stream', 'stdout',
                            '--search', 'error[E0308]', '--lines', '1'], env=env, capture_output=True, text=True,
                           check=True)
    if 'E0308' not in found.stdout:
        raise RuntimeError('Ω-inspect did not find the failure')
    out['build_log'] = result([native_out], [ran.stdout, found.stdout],
                              native_error_visible='E0308' in native_out,
                              omega_error_visible=True)

    new_session()
    evidence_log = Path(json.loads(ran.stdout)['evidence']) / 'stdout.log'
    text, allowed = omega_read(evidence_log)
    if allowed or 'Ω-inspect' not in text:
        raise RuntimeError('evidence Read was not redirected')
    out['evidence_read'] = result([numbered(evidence_log)], [text, found.stdout],
                                  native_error_visible='E0308' in numbered(evidence_log),
                                  omega_error_visible=True)
    return out


def guard_table(rows):
    lines = ['| Scenario | Plain Claude Code tokens | omega tokens | Change |',
             '| --- | ---: | ---: | ---: |']
    total_native = total_omega = 0
    for key, label in GUARD_WORKLOADS.items():
        row = rows[key]
        native, omega = row['native_tokens'], row['omega_tokens']
        if native is None:
            native, omega, unit = row['native_chars'], row['omega_chars'], ' chars'
        else:
            unit = ''
        total_native += native
        total_omega += omega
        note = ''
        if 'native_error_visible' in row and not row['native_error_visible']:
            note = ' (plain: failure not shown)'
        lines.append(f'| {label}{note} | {native:,}{unit} | {omega:,}{unit} | {(omega - native) / native:+.0%} |')
    lines.append(f'| **Total** | **{total_native:,}** | **{total_omega:,}** | '
                 f'**{(total_omega - total_native) / total_native:+.0%}** |')
    return '\n'.join(lines)


def tokenizer_file(path):
    """Return the pinned GPT-2 tokenizer, downloading it once into the cache if needed."""
    if path is None:
        path = TOKENIZER_CACHE
        if not path.exists():
            import urllib.request
            data = urllib.request.urlopen(TOKENIZER_URL, timeout=60).read()
            if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
                raise RuntimeError(f'{TOKENIZER_URL}: unexpected sha256')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest() != TOKENIZER_SHA256:
        print(f'warning: {path} is not the pinned GPT-2 tokenizer; counts are not comparable', file=sys.stderr)
    return path


def table(medians):
    rows = ['| Workload | Returned tokens before → after | Reads before → after | Local latency ms before → after |',
            '| --- | ---: | ---: | ---: |']
    for key, label in WORKLOADS.items():
        b, a = medians['before'][key], medians['after'][key]
        cell = lambda name, fmt='{:g}': (f'{fmt.format(b[name])} → {fmt.format(a[name])}'
                                         if b.get(name) is not None else 'n/a')
        rows.append(f'| {label} | {cell("returned_tokens")} | {cell("reads")} | {cell("latency_ms", "{:.2f}")} |')
    if medians['before']['preview'].get('returned_tokens') is not None:
        before = sum(medians['before'][k]['returned_tokens'] for k in WORKLOADS)
        after = sum(medians['after'][k]['returned_tokens'] for k in WORKLOADS)
        rows.append(f'\nTotal returned tokens: {before:g} → {after:g} ({(after - before) / before:+.0%}).')
    return '\n'.join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before-ref', default=BASELINE_REF,
                        help='commit whose run/optimize are built as the baseline (default: published baseline)')
    parser.add_argument('--after-ref', default='HEAD', help='commit built as the candidate (default: HEAD)')
    for flag in ('before-run', 'after-run', 'before-optimize', 'after-optimize', 'context-guard'):
        parser.add_argument('--' + flag, type=Path, help='use this prebuilt binary instead of building the ref')
    parser.add_argument('--output', type=Path,
                        help='new report file (default: ~/.agents/tasks/reports/omega-benchmark/<UTC>.json)')
    parser.add_argument('--tokenizer-json', type=Path,
                        help=f'local tokenizer JSON (default: pinned GPT-2, cached in {TOKENIZER_CACHE})')
    parser.add_argument('--no-tokens', action='store_true', help='skip token counts')
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 50:
        parser.error('--repeats must be 1..50')
    if args.output is None:
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        args.output = Path.home() / f'.agents/tasks/reports/omega-benchmark/{stamp}.json'
        args.output.parent.mkdir(parents=True, exist_ok=True)
    tokenizer = None
    if not args.no_tokens:
        try:
            from tokenizers import Tokenizer  # optional existing measurement tool only
        except ImportError:
            parser.error('tokenizers is missing: run with `uv run --no-project --with tokenizers python3 ...` or pass --no-tokens')
        args.tokenizer_json = tokenizer_file(args.tokenizer_json)
        tokenizer = Tokenizer.from_file(str(args.tokenizer_json))

    def count_tokens(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids) if tokenizer else None

    report: dict = {'method': 'synthetic local replay; medians, no model calls or automatic read suppression',
              'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'platform': platform.platform(), 'python': sys.version,
              'tokenizer_sha256': (hashlib.sha256(args.tokenizer_json.read_bytes()).hexdigest()
                                   if tokenizer else None),
              'token_scope': 'local tokenizer counts of returned JSON and commands, not provider telemetry',
              'repeats': args.repeats, 'refs': {}, 'binaries': {}, 'samples': {}}
    with tempfile.TemporaryDirectory(prefix='omega-context-bench-') as temp:
        root = Path(temp)
        for label in ('before', 'after'):
            built_run = getattr(args, label + '_run')
            built_optimize = getattr(args, label + '_optimize')
            if built_run is None or built_optimize is None:
                ref = getattr(args, label + '_ref')
                commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', ref + '^{commit}'],
                                        capture_output=True, text=True, check=True).stdout.strip()
                report['refs'][label] = commit
                print(f'building {label} from {commit[:12]}', file=sys.stderr)
                release = build(commit, root / f'{label}-src')
                built_run = built_run or release / 'run'
                built_optimize = built_optimize or release / 'optimize'
                if label == 'after' and args.context_guard is None:
                    args.context_guard = release / 'context_guard'
            # Fixed names make the replay policy explicit and command lengths comparable.
            run = root / f'{label}-run'
            optimize = root / f'{label}-optimize'
            run.symlink_to(built_run.resolve())
            optimize.symlink_to(built_optimize.resolve())
            report['binaries'][label] = {key: hashlib.sha256(path.read_bytes()).hexdigest()
                                         for key, path in [('run', run), ('optimize', optimize)]}
            report['samples'][label] = []
            for i in range(args.repeats):
                home = root / f'{label}-{i}'
                home.mkdir()
                report['samples'][label].append(replay(run, optimize, home, count_tokens))
        guard = args.context_guard or ROOT / 'rust/target/release/context_guard'
        report['binaries']['after']['context_guard'] = hashlib.sha256(guard.read_bytes()).hexdigest()
        home = root / 'native-vs-omega'
        home.mkdir()
        report['native_vs_omega'] = native_vs_omega(root / 'after-run', guard, home, count_tokens)
    report['medians'] = {}
    for label, samples in report['samples'].items():
        report['medians'][label] = {
            workload: {key: statistics.median([s[workload][key] for s in samples])
                       for key, value in samples[0][workload].items()
                       if isinstance(value, (int, float))}
            for workload in samples[0]}
    # Exclusive creation protects existing evidence.
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(f'Report: {args.output}\n')
    print('## Plain Claude Code tools vs omega (deterministic)\n')
    print(guard_table(report['native_vs_omega']))
    print(f'\n## Baseline {report["refs"].get("before", "binaries")[:12]} vs '
          f'{report["refs"].get("after", "binaries")[:12]} (medians of {args.repeats} replays)\n')
    print(table(report['medians']))


if __name__ == '__main__':
    main()
