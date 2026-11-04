Native evidence executor (Linux/POSIX)
====================================

Build:

    cc -std=c11 -O2 -Wall -Wextra -Werror -o run run.c

Use the native binary directly to avoid Python startup:

    /absolute/path/to/run run --timeout 120 -- command arguments

`run.py` is an optional legacy launcher for Python callers and tests; it adds
Python startup and immediately replaces itself with `run`.

The native path executes `run` with the original CLI flags and writes complete,
private stdout.log/stderr.log/result.json evidence under ~/.agents/tasks/evidence.
It retains original exit/status meanings, signal forwarding, process group
cleanup, a separate sentinel for wrapper SIGKILL, noninteractive stdin, bounded
inline output and a 25 ms output-limit/timeout monitoring interval. SIGCHLD wakes
the monitor immediately when a command exits. The sentinel is forked without a
second interpreter. Children are gated until the sentinel exists.

`run --native-only ...` (flag first) disables compatibility fallback and returns
exit 2 on unsupported inputs. Narrow trusted capability launchers must use this
mode with fixed HOME/PATH/cwd. Rich output diagnostics still work through pinned
metadata/stdout/stderr descriptors; they never reopen mutable evidence names.
Evidence directories and writes are anchored to nofollow directory descriptors
under the configured tasks root. Queued control signals are drained before
status selection and stay blocked through finalization to prevent RUNNING-only
evidence when signals race command exit.

`inspect`, `prune`, unknown options/abbreviations, unusual filesystem encoding and
invalid working directories defer to adjacent `run-original.py`. Rich diagnostic
extraction, ANSI removal, Unicode/invalid UTF-8 output, and JSON-escaped inline
output exceeding the 4,000-byte budget use adjacent `finalize.py`, which imports
the unchanged original diagnostic helpers. Both fallback paths use `python3 -I`.
Simple output and successful verbose commands require no Python postprocessing.

This executor is not an authorization boundary and confers no permissions. Never
approve it as a broad executable prefix: it intentionally accepts arbitrary argv
and shell payloads. Hook/native sandbox approvals remain separate. Only bounded,
argument-validated capability entry points may receive narrow approval rules.

Install this directory as a unit so the binary and compatibility files remain
adjacent. Preserve the original global run.py for rollback. No global files have
been installed or changed by this implementation.

Validation:

    python3 test_run.py
    python3 benchmark.py

The first runs the existing 21 behavioral tests through the native compatibility
launcher. The benchmark invokes the binary directly, randomizes paired original,
native and direct commands (60 samples plus 3 warmups each), and writes every
wall-clock sample, exact command, environment and SHA256 to benchmark.json.

Additional checks:

    python3 -m unittest discover -s . -p 'test_*.py'

These add five boundary tests, including 20 signal/exit races, symlink evidence
root rejection without permission changes/writes, fail-closed native-only input,
Unicode/invalid UTF-8/ANSI output and invalid-cwd compatibility.
