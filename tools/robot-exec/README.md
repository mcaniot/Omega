# Local robotics execution

Use `'Ω-run' --timeout 120 -- COMMAND ARGS` for
general development. The compiled POSIX executor captures full stdout/stderr in
private evidence files and returns bounded JSON. It preserves timeout (124), log
limit (125), interruption/crash (128+signal), group cleanup, and independent
sentinel behavior. The executor is one Rust binary (`rust/`) that also implements diagnostics, `inspect`
and `prune`; there is no Python fallback.
There is no generic executor/interpreter outside-sandbox allow rule.

The native `shell_guard` Bash hook verifies canonical quoting and requests approval for remote
access, robot-related ROS 2/Zenoh commands, destructive Git operations, uploads
and credential paths. Without flags it never returns `allow`; the Claude hook passes
`--allow-clean`, so a fully checked wrapper call matching no deny/ask policy is allowed
without a native prompt (Codex keeps the flagless form). Arbitrary
execution tools do not acquire hook coverage by using the same machine; their
sandbox and approval checks still apply. Do not claim static command regexes
can prove an arbitrary program will never contact a robot.

Codex remains workspace-write, on-request, network disabled. Ordinary builds,
tests and scripts remain sandboxed. Existing unrelated integration hooks remain.
Restart Codex/Claude to reload changed user rules and discard old session grants.

The AppArmor exception applies only to the installed bundled Bubblewrap helper
and grants user-namespace creation. No global sysctl, AppArmor mode, host network,
or sandbox writable roots are relaxed. A Codex upgrade moving its helper requires
reviewing and updating the exact profile path. Root installation may require
administrator authentication.

Sources are versioned in the omega repository (`rust/`); `install.py --apply` builds
and installs `native/run` as a real file that is not versioned. To remove the
AppArmor profile, run `apparmor_parser -R` before deleting its file. Do not prune
evidence without explicit authorization.
