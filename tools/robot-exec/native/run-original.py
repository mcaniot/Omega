#!/usr/bin/env python3
"""Capture command evidence before returning bounded diagnostics to an agent."""
import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path.home() / '.agents' / 'tasks' / 'evidence'
# Ranked tiers: explicit failures, then error words, then bare source locations (glog
# prefixes, warnings and traceback frames are context, never a reason to hide an error).
# Whole words only, so metrics such as "tracking_error=" do not match.
TIERS = [
    re.compile(r'Traceback|\b[A-Z]\w*(?:Error|Exception)\b|^E\s|^(?:FAILED|ERROR|FATAL)\b'
               r'|(?i:\[(?:error|fatal)\])|\berror:|[Ss]egmentation fault|core dumped|\bpanicked\b'
               r'|terminate called|\bwhat\(\):|Assertion .* failed|\bAborted\b'),
    re.compile(r'\b(?:errors?|exception|fatal|failed|failure|abort(?:ed)?)\b', re.I),
    re.compile(r'\b[^\s:]+\.(?:py|c|cc|cpp|cu|h|hpp):\d+|File ".+", line \d+'),
]
EDGE = 4  # first and last matches kept per tier
LIMIT = 8  # diagnostics returned in total
SENTINEL = '''import os, signal, sys, time
group, grace = int(sys.argv[1]), float(sys.argv[2])
if sys.stdin.buffer.read() != b"done":  # wrapper died without finalizing
    try:
        os.killpg(group, signal.SIGTERM)
        time.sleep(grace)
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
'''


def private(path, flags):
    return os.open(path, flags, 0o600)
PASSED = re.compile(r'\s(?:PASSED|SKIPPED|XFAIL)\b|\[\s*OK\s*\]')  # test names may contain "failure"
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', encoding='utf-8', errors='surrogateescape', opener=private) as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def chunks(path):
    """Yield bounded portions of physical lines, retaining source locations."""
    with path.open('rb') as source:
        line = 1
        while True:
            offset = source.tell()
            chunk = source.readline(8192)
            if not chunk:
                break
            yield line, offset, chunk
            line += chunk.endswith(b'\n')


class Edges:
    """Keep the first and last distinct matches, so a root cause is not evicted by repeats."""

    def __init__(self):
        self.first, self.last, self.seen = [], collections.deque(maxlen=EDGE), set()

    def add(self, item):
        key = item['text']
        if key in self.seen:
            return
        self.seen.add(key)
        (self.first if len(self.first) < EDGE else self.last).append(item)
        # Bound memory even for millions of distinct error messages.
        if len(self.seen) > 256:
            self.seen = {entry['text'] for entry in self.first + list(self.last)}

    def items(self):
        return self.first + list(self.last)


def excerpts(directory):
    """Return diagnostics ordered by importance (callers drop from the end)."""
    tiers = [Edges() for _ in TIERS]
    tail = collections.deque(maxlen=3)
    for stream in ('stdout', 'stderr'):
        for line, offset, raw in chunks(directory / (stream + '.log')):
            text = ANSI.sub('', raw.decode('utf-8', 'replace')).strip()
            text = ''.join(c if c.isprintable() else ' ' for c in text)
            if not text:
                continue
            item = {'stream': stream, 'line': line, 'offset': offset, 'text': text[:220]}
            tail.append(item)
            if PASSED.search(text):
                continue
            for pattern, edges in zip(TIERS, tiers):
                if pattern.search(text):
                    edges.add(item)
                    break
    selected = [item for edges in tiers for item in edges.items()][:LIMIT]
    return selected or list(tail)


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def run(args):
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if args.shell is not None:
        if command:
            raise ValueError('Use either --shell or argv after --, not both')
        command = [args.shell_executable, '-c', args.shell]
    if not command:
        raise ValueError('A command is required after --')
    if args.timeout is not None and args.timeout <= 0:
        raise ValueError('--timeout must be positive')
    if args.kill_after <= 0 or args.max_log_bytes <= 0:
        raise ValueError('--kill-after and --max-log-bytes must be positive')
    cwd = str(Path(args.cwd).resolve())
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ROOT.stat().st_mode & 0o077:
        ROOT.chmod(0o700)  # evidence may contain secrets printed by commands
    directory = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S-'), dir=ROOT))
    start = time.monotonic()
    metadata = {'schema': 1, 'status': 'RUNNING', 'command': command, 'cwd': cwd,
                'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'wrapper_pid': os.getpid(), 'timeout_seconds': args.timeout,
                'stdout': str(directory / 'stdout.log'), 'stderr': str(directory / 'stderr.log')}
    write_json(directory / 'result.json', metadata)
    received = []
    process = None
    old_handlers = {}

    def interrupted(sig, _frame):
        if not received:
            received.append(sig)
        if process is not None:
            signal_group(process.pid, sig)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        old_handlers[sig] = signal.signal(sig, interrupted)
    timed_out = False
    log_limited = False
    stop_time = None
    cleanup = False
    sentinel = None
    try:
        with open(directory / 'stdout.log', 'wb', opener=private) as out, \
                open(directory / 'stderr.log', 'wb', opener=private) as err:
            try:
                process = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                err.write((f'{type(exc).__name__}: {exc}\n').encode())
                returncode = 127 if isinstance(exc, FileNotFoundError) else 126
            else:
                metadata['pid'] = process.pid
                metadata['process_group'] = process.pid
                write_json(directory / 'result.json', metadata)
                # The child runs in its own session, so killing the wrapper (even SIGKILL)
                # would orphan it; an independent sentinel stops the group in that case.
                sentinel = subprocess.Popen([sys.executable, '-I', '-c', SENTINEL, str(process.pid), str(args.kill_after)],
                                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
                while process.poll() is None:
                    elapsed = time.monotonic() - start
                    if not received and args.timeout is not None and elapsed >= args.timeout and not timed_out:
                        timed_out = True
                        signal_group(process.pid, signal.SIGTERM)
                    if not (received or timed_out or log_limited) and max(
                            os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > args.max_log_bytes:
                        log_limited = True  # runaway output would fill the disk
                        signal_group(process.pid, signal.SIGTERM)
                    if (received or timed_out or log_limited) and stop_time is None:
                        stop_time = time.monotonic()
                    if stop_time is not None and time.monotonic() - stop_time >= args.kill_after:
                        signal_group(process.pid, signal.SIGKILL)
                    time.sleep(0.025)
                returncode = process.wait()
                # A captured command owns its process group: do not leave background
                # writers changing logs after the result has been finalized.
                if group_alive(process.pid):
                    cleanup = True
                    signal_group(process.pid, signal.SIGTERM)
                    deadline = time.monotonic() + args.kill_after
                    while group_alive(process.pid) and time.monotonic() < deadline:
                        time.sleep(0.025)
                    signal_group(process.pid, signal.SIGKILL)
    finally:
        if sentinel is not None:
            sentinel.communicate(b'done')
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    crashed = returncode < 0 and not (received or timed_out or log_limited)
    code = 124 if timed_out else (125 if log_limited else (128 + received[0] if received else (128 - returncode if returncode < 0 else returncode)))
    if timed_out or log_limited:
        status = 'TIMEOUT' if timed_out else 'LOG_LIMIT'
    elif received:
        status = 'INTERRUPTED'
    elif crashed:
        status = 'CRASHED'  # the child died from its own signal (segfault, abort, OOM kill)
    else:
        status = 'PASS' if code == 0 else 'FAIL'
    sizes = {stream: (directory / (stream + '.log')).stat().st_size for stream in ('stdout', 'stderr')}
    metadata.update(status=status, exit_code=code, child_returncode=returncode,
                    duration_seconds=round(time.monotonic() - start, 4),
                    finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    interrupted_signal=received[0] if received else None,
                    signal=signal.Signals(-returncode).name if crashed else None,
                    background_children_terminated=cleanup, bytes=sizes)
    write_json(directory / 'result.json', metadata)
    result = {key: metadata[key] for key in ('status', 'exit_code', 'duration_seconds', 'bytes')}
    if crashed:
        result['signal'] = metadata['signal']
    result['evidence'] = str(directory)
    if sum(sizes.values()) <= args.inline_bytes:
        # Small output is cheaper inline than a second read; the files stay authoritative.
        for stream in ('stdout', 'stderr'):
            text = ANSI.sub('', (directory / (stream + '.log')).read_bytes().decode('utf-8', 'replace'))
            if text:
                result[stream] = text
        result['diagnostics'] = []
    if len(encoded(result)) > 4000 or 'diagnostics' not in result:
        # JSON escaping can inflate control bytes; fall back to bounded excerpts.
        result.pop('stdout', None)
        result.pop('stderr', None)
        result['diagnostics'] = excerpts(directory) if code else []
    while len(encoded(result)) > 4000 and result['diagnostics']:
        result['diagnostics'].pop()  # least important first
    result['diagnostics'].sort(key=lambda item: (item['stream'], item['line']))
    print(encoded(result).decode())
    return code


def inspect(args):
    if args.offset < 0 or not 1 <= args.lines <= 40 or not 1 <= args.max_bytes <= 7000:
        raise ValueError('Require offset >= 0, lines 1..40, max-bytes 1..7000')
    needle = args.search.encode('utf-8') if args.search is not None else None
    if needle is not None and not 1 <= len(needle) <= 512:
        raise ValueError('--search must contain 1..512 UTF-8 bytes')
    directory = Path(args.evidence).resolve()
    path = directory / (args.stream + '.log')
    result = {'evidence': str(directory), 'stream': args.stream, 'offset': args.offset,
              'next_offset': args.offset, 'fragments': [], 'eof': False}
    used = 0
    overlap = b''
    with path.open('rb') as source:
        source.seek(args.offset)
        while len(result['fragments']) < args.lines and used < args.max_bytes:
            offset = source.tell()
            raw = source.readline(min(1024, args.max_bytes - used))
            if not raw:
                result['eof'] = True
                break
            window = overlap + raw
            match = window.find(needle) if needle is not None else -1
            if needle is None or match >= 0:
                if needle is not None:
                    begin = max(0, match - 100)
                    fragment = window[begin:match + len(needle) + 100]
                    fragment_offset = offset - len(overlap) + begin
                else:
                    fragment, fragment_offset = raw, offset
                entry = {'offset': fragment_offset, 'text': fragment.decode('utf-8', 'replace')}
                result['fragments'].append(entry)
                if len(encoded(result)) > 7600:
                    result['fragments'].pop()
                    source.seek(offset)
                    break
                used += len(fragment)
            overlap = window[-(len(needle) - 1):] if needle is not None and len(needle) > 1 else b''
            result['next_offset'] = source.tell()
        result['eof'] = source.tell() >= os.fstat(source.fileno()).st_size
    print(encoded(result).decode())
    return 0


def prune(args):
    """Report (or with --apply delete) evidence older than --days, keeping referenced runs."""
    if args.days <= 0:
        raise ValueError('--days must be positive')
    name = r'\d{8}T\d{6}-(?:native-[A-Za-z0-9]{6}|[\w-]{8})'  # Python tempfile or native/run mkdtemp
    referenced = set()
    for task in (ROOT.parent / 'active').glob('*.md'):
        referenced.update(re.findall(name, task.read_text(errors='replace')))
    cutoff = time.time() - args.days * 86400
    candidates, size = [], 0
    for directory in ROOT.iterdir():
        if (directory.is_dir() and not directory.is_symlink() and directory.name not in referenced
                and re.fullmatch(name, directory.name)
                and directory.stat().st_mtime < cutoff):
            candidates.append(directory)
            size += sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
    if args.apply:
        for directory in candidates:
            shutil.rmtree(directory)
    print(json.dumps({'status': 'PASS', 'applied': args.apply, 'runs': len(candidates), 'bytes': size,
                      'kept_referenced': len(referenced)}))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    execute = sub.add_parser('run', help='Capture complete output and return one JSON result; noninteractive')
    execute.add_argument('--cwd', default=os.getcwd())
    execute.add_argument('--timeout', type=float)
    execute.add_argument('--shell')
    execute.add_argument('--shell-executable', default='/bin/bash')
    execute.add_argument('--inline-bytes', type=int, default=2048,
                         help='Inline stdout/stderr when their total size is at most this (0 disables)')
    execute.add_argument('--kill-after', type=float, default=10,
                         help='Seconds between SIGTERM and SIGKILL (lets sims finalize MP4/HDF5)')
    execute.add_argument('--max-log-bytes', type=int, default=2 << 30,
                         help='Stop the command (status LOG_LIMIT, exit 125) when one stream exceeds this')
    execute.add_argument('command', nargs=argparse.REMAINDER)
    expand = sub.add_parser('inspect', help='Bounded byte-addressed diagnostic expansion; offsets allow pagination')
    expand.add_argument('evidence')
    expand.add_argument('--stream', choices=('stdout', 'stderr'), default='stderr')
    expand.add_argument('--offset', type=int, default=0)
    expand.add_argument('--lines', type=int, default=20)
    expand.add_argument('--max-bytes', type=int, default=4096)
    expand.add_argument('--search', help='Literal substring filter; use short distinctive terms')
    clean = sub.add_parser('prune', help='Dry-run by default: old evidence not referenced by tasks/active')
    clean.add_argument('--days', type=float, default=14)
    clean.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        return {'run': run, 'inspect': inspect, 'prune': prune}[args.operation](args)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'FAIL', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
