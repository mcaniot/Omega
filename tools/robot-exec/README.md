# Local robotics execution

Use `/home/mcaniot/.agents/tools/robot-exec/native/run run --timeout 120 -- COMMAND ARGS` for
general development. The compiled POSIX executor captures full stdout/stderr in
private evidence files and returns bounded JSON. It preserves timeout (124), log
limit (125), interruption/crash (128+signal), group cleanup, and independent
sentinel behavior. The executor is one Rust binary (`rust/`) that also implements diagnostics, `inspect`
and `prune`; there is no Python fallback.
There is no generic executor/interpreter outside-sandbox allow rule.

For trusted source reads use `/home/mcaniot/.agents/tools/robot-exec/local-read`:

- `status`: fixed read-only Git status for this checkout; external fsmonitor and hooks disabled.
- `files`: fixed `rg --files` for this checkout; no supplied options or programs.
- `text FILE [START [COUNT]]`: up to 40 lines from an approved regular source file.
- `find LITERAL FILE`: up to 40 matching source lines, with no regex or shell evaluation.

Read roots are this checkout and `~/.agents/tools`. Hidden/sensitive paths,
hardlinks, non-owned files, files over 16 MiB and outside roots are rejected.
Symlink aliases are resolved and the final open is anchored beneath a trusted
root with Linux openat2. Text results are capped at 64 KiB and then captured by
the executor (default inline bound 2 KiB). Use normal approval for unsupported
or sensitive reads. This capability fixes HOME, PATH and cwd; `--native-only`
prevents falling into the generic legacy execution path. Internal helper verbs
are not covered by allow rules.

The native `shell_guard` Bash hook verifies canonical quoting and requests approval for remote
access, robot-related ROS 2/Zenoh commands, destructive Git operations, uploads
and credential paths. It never returns an unconditional `allow`. Arbitrary
execution tools do not acquire hook coverage by using the same machine; their
sandbox and approval checks still apply. Do not claim static command regexes
can prove an arbitrary program will never contact a robot.

Codex remains workspace-write, on-request, network disabled. Scoped capability
rules authorize only the four read verbs outside the sandbox. Ordinary builds,
tests and scripts remain sandboxed. Existing unrelated integration hooks remain.
Restart Codex/Claude to reload changed user rules and discard old session grants.

The AppArmor exception applies only to the installed bundled Bubblewrap helper
and grants user-namespace creation. No global sysctl, AppArmor mode, host network,
or sandbox writable roots are relaxed. A Codex upgrade moving its helper requires
reviewing and updating the exact profile path. Root installation may require
administrator authentication.

Sources are versioned in the omega repository (`rust/`); `install.py --apply` builds
and installs `local-read` and `native/run` as real files that are not versioned. Text
files under `~/.agents/tools` are symlinks into the repository, so `local-read` (rooted
at `~/.agents/tools`) rejects reading them; read them from the repository instead.
`test_local_read.py` builds `local_read` with throwaway roots next to a stub runner and
replays `fixtures/local_read_golden.json.gz`, recorded from the original launcher.
To remove the
AppArmor profile, run `apparmor_parser -R` before deleting its file. Do not prune
evidence without explicit authorization.
