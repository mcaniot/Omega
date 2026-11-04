Native evidence executor
========================

`run` is a Rust binary (`rust/src/bin/run/`, crate `omega-tools`) installed as
`~/.agents/tools/robot-exec/native/run`. It has no Python or other fallback.
The installer exposes `Ω-run`, `Ω-inspect`, `Ω-prune` and `Ω-read`
as links in `~/.local/bin` (keep this directory on PATH). `Ω-run` selects the
run operation directly; the original absolute commands remain supported.

Build and install (from the repository root):

    python3 install.py --apply        # cargo build --release --locked, then atomic 0700 install
    cargo build --release --locked    # in rust/, build only

Usage:

    'Ω-run' --timeout 120 -- command arguments
    'Ω-run' --shell 'script'
    'Ω-inspect' EVIDENCE --stream stderr --lines 20
    'Ω-prune' [--days N] [--apply]

It writes complete, private stdout.log/stderr.log/result.json evidence under
~/.agents/tasks/evidence. It keeps exit/status meanings (timeout 124, log limit 125,
128+signal), signal forwarding, process group cleanup, a separate sentinel for wrapper
SIGKILL, noninteractive stdin, bounded inline output and a 25 ms output-limit/timeout
monitoring interval. SIGCHLD wakes the monitor immediately when a command exits.
Children are gated until the sentinel exists.

Combined stdout/stderr up to 8 KiB is returned inline by default (`--inline-bytes N`,
0 disables), within a 12 KiB JSON budget. Large JSON expansions fall back to the
summary; failure diagnostics retain their separate 4,000-byte budget. Complete logs
are always preserved. Reuse inline output; expand larger logs with targeted `inspect`
slices rather than reading a log over 8 KiB in full.

Evidence directories and writes are anchored to nofollow directory descriptors under the
configured tasks root. Queued control signals are drained before status selection and stay
blocked through finalization, so signals racing command exit cannot leave RUNNING-only
evidence. Rich output diagnostics, ANSI removal, Unicode/invalid UTF-8 output and inline
output requiring the full JSON encoder are finalized in-process.

`run --native-only ...` (flag first) returns exit 2 on inputs the native path does not
support (abbreviations, `--opt=value`, usage/help, unusual filesystem encoding, invalid
working directories). Narrow trusted capability launchers such as `local-read` must use it
with fixed HOME/PATH/cwd.

This executor is not an authorization boundary and confers no permissions. Never approve it
as a broad executable prefix: it intentionally accepts arbitrary argv and shell payloads.
Hook/native sandbox approvals remain separate.

Tests (default binary is the deployed one; `OMEGA_RUN_BINARY` selects another, for example
`rust/target/release/run`):

    python3 -m pytest tools hooks

- `tools/test_run.py`: behavior of the executor.
- `native/test_boundary.py`: signal/exit races, symlink evidence-root rejection, fail-closed
  native-only input, Unicode/invalid UTF-8/ANSI output, invalid-cwd handling.
- `native/test_golden.py`: replays `native/fixtures/run_golden.json.gz`, outputs recorded from
  the original implementation (volatile values normalized).
