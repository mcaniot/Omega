# Development

How to test a change and cut a release.

## Tests

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
python3 -m pytest tools hooks
```

The GitHub Actions `Tests` workflow builds fresh binaries and runs the Rust and Python checks on
every push, pull request or manual dispatch.

Tests drive the binaries and replay golden fixtures recorded from the original implementations.
Some default to the **deployed** binary, so to test a change before installing it, point the
overrides at `rust/target/release/<name>`:

| Test | Default binary (override) |
| --- | --- |
| `tools/test_run.py`, `tools/robot-exec/native/test_{boundary,golden}.py` | deployed `run` (`OMEGA_RUN_BINARY`) |
| `tools/test_shell_guard.py` | deployed `shell_guard` (`OMEGA_SHELL_GUARD_BINARY`) |
| `hooks/test_session_name.py` | deployed `session_name` (`OMEGA_SESSION_NAME_BINARY`) |
| `tools/test_context_guard.py` | `rust/target/release/context_guard` (`OMEGA_CONTEXT_GUARD_BINARY`) |
| `tools/test_context_budget.py` | `rust/target/release/context_budget` (`OMEGA_CONTEXT_BUDGET_BINARY`) |
| `tools/test_optimize.py`, `tools/test_version.py` | `rust/target/release/optimize` (`OMEGA_OPTIMIZE_BINARY`) |
| `tools/test_omega_commands.py` | release `run` and `shell_guard`: command prefixes, installer links, dispatch, guard policy |
| `tools/robot-exec/test_local_read.py` | builds its own binary with throwaway roots |
| `tools/test_response_settings.py` | installer only: response settings, conflicts, idempotence, config symlinks |

## Release

omega follows [Semantic Versioning](https://semver.org/). The `version` in `rust/Cargo.toml` is
the single source: `omega --version` prints it, and `tools/test_version.py` checks that
`rust/Cargo.lock` and the latest `CHANGELOG.md` release match it.

1. In `CHANGELOG.md`, move the `[Unreleased]` entries under `## [X.Y.Z] - YYYY-MM-DD` and update
   the compare links at the bottom.
2. Set `version = "X.Y.Z"` in `rust/Cargo.toml`, then run
   `cargo build --release --manifest-path rust/Cargo.toml` once, without `--locked`, to update
   `rust/Cargo.lock`.
3. Run the [tests](#tests) and commit.
4. Tag and push:

   ```sh
   git tag -a vX.Y.Z -m "omega vX.Y.Z"
   git push origin main vX.Y.Z
   ```

5. The `Release` workflow checks that the tag matches `rust/Cargo.toml` and has a changelog entry,
   then creates the GitHub Release with that entry as its notes.
6. Deploy on each machine with `python3 install.py --apply`.
