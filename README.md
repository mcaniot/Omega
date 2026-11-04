# omega

Versioned source of my Claude Code and Codex agent setup. The goal is to keep it small and limit
context rot: one short instruction file, a few agents, skills loaded on demand, and command output
kept on disk instead of in the context.

The previous Omega project (memory, code graph, dashboard) is archived under the `omega-legacy` tag.

## Layout

| Path | Deployed to | Role |
| --- | --- | --- |
| `AGENTS.md` | `~/.agents/AGENTS.md` (`~/.claude/CLAUDE.md` imports it, `~/.codex/AGENTS.md` links to it) | Global instructions, loaded in every session |
| `agents/*.md` | `~/.claude/agents/` (link), `~/.codex/agents/*.toml` (generated) | The 4 workers: scout, coder, architect, robotics-rl |
| `skills/*/SKILL.md` | `~/.agents/skills/` (Codex scans it), `~/.claude/skills/` | Only the description is loaded until the skill is used |
| `rust/` | `~/.agents/tools/robot-exec/native/run`, `~/.agents/tools/robot-exec/local-read`, `~/.agents/tools/shell_guard`, `~/.claude/hooks/session_name` | Cargo crate `omega-tools`: evidence executor, trusted read launcher, PreToolUse guard (Claude + Codex), session naming hook. Built and installed as real 0700 files by `install.py --apply` |
| `tools/`, `hooks/` | `~/.agents/tools/`, `~/.claude/hooks/` | Docs, tests and golden fixtures for those binaries (tests and fixtures are not linked) |
| `docs/` | — | Notes (backend audit) |

Runtime state stays outside the repository in `~/.agents/tasks/` (`active/`, `completed/`, `evidence/`).

## Install / update

```sh
python3 install.py            # dry run: lists what would change
python3 install.py --apply    # create links, regenerate the Codex agents, build and install the Rust binaries
python3 install.py --budget   # bytes loaded into every session
```

Sources are symlinked: edits to `AGENTS.md`, agents and skills take effect at the next session, but
The Rust binaries run on every tool call; a build is only installed by `install.py --apply`, so run the tests
first. Keep this checkout on a branch that contains these files. An existing file
is replaced only if it is already a link or has identical content; otherwise it is reported as `CONFLICT`
and left untouched.

The Codex model of each agent is set in `CODEX_AGENTS` (`install.py`). Adding an agent also requires an
`[agents.<name>]` entry in `~/.codex/config.toml`. Claude skills listed in `CLAUDE_SKIP_SKILLS` are visible
only to Codex.

## Context rules

- Anything added to `AGENTS.md`, an agent description or a skill description is paid for in every session:
  check `install.py --budget` before and after.
- Procedures go in a skill (loaded on demand), not in `AGENTS.md`.
- Long output goes through the native `run` executor: complete log on disk, bounded JSON in the context.

## Tests

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
python3 -m pytest tools hooks
```

Each tool has one test file that drives its binary, replaying golden fixtures recorded from the original
implementations: `tools/test_run.py`, `tools/robot-exec/native/test_{boundary,golden}.py`,
`tools/test_shell_guard.py`, `hooks/test_session_name.py`, `tools/robot-exec/test_local_read.py`. By default
they test the deployed binary; set `OMEGA_RUN_BINARY`, `OMEGA_SHELL_GUARD_BINARY` or
`OMEGA_SESSION_NAME_BINARY` (for example to `rust/target/release/<name>`) to test a fresh build first.
`test_local_read.py` always builds its own binary with throwaway roots.
