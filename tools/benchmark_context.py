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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('before-run', 'after-run', 'before-optimize', 'after-optimize', 'output'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--tokenizer-json', type=Path)
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 50:
        parser.error('--repeats must be 1..50')
    tokenizer = None
    if args.tokenizer_json:
        from tokenizers import Tokenizer  # optional existing measurement tool only
        tokenizer = Tokenizer.from_file(str(args.tokenizer_json))

    def count_tokens(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids) if tokenizer else None

    report = {'method': 'synthetic local replay; medians, no model calls or automatic read suppression',
              'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'platform': platform.platform(), 'python': sys.version,
              'tokenizer_sha256': (hashlib.sha256(args.tokenizer_json.read_bytes()).hexdigest()
                                   if tokenizer else None),
              'token_scope': 'local tokenizer counts of returned JSON and commands, not provider telemetry',
              'repeats': args.repeats, 'binaries': {}, 'samples': {}}
    with tempfile.TemporaryDirectory(prefix='omega-context-bench-') as temp:
        root = Path(temp)
        for label in ('before', 'after'):
            # Fixed names make the replay policy explicit and command lengths comparable.
            run = root / f'{label}-run'
            optimize = root / f'{label}-optimize'
            run.symlink_to(getattr(args, label + '_run').resolve())
            optimize.symlink_to(getattr(args, label + '_optimize').resolve())
            report['binaries'][label] = {key: hashlib.sha256(path.read_bytes()).hexdigest()
                                         for key, path in [('run', run), ('optimize', optimize)]}
            report['samples'][label] = []
            for i in range(args.repeats):
                home = root / f'{label}-{i}'
                home.mkdir()
                report['samples'][label].append(replay(run, optimize, home, count_tokens))
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
    print(json.dumps({'report': str(args.output), 'medians': report['medians']}))


if __name__ == '__main__':
    main()
