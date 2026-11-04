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
| `tools/run.py`, `tools/shell_guard.py` | `~/.agents/tools/` | Evidence wrapper and PreToolUse guard (Claude + Codex) |
| `tools/robot-exec/` | `~/.agents/tools/robot-exec/` | Sources of the native executor and `local-read`; binaries are compiled in place and not versioned |
| `hooks/session_name.py` | `~/.claude/hooks/` | Session naming hook |
| `docs/` | — | Notes (backend audit) |

Runtime state stays outside the repository in `~/.agents/tasks/` (`active/`, `completed/`, `evidence/`).

## Install / update

```sh
python3 install.py            # dry run: lists what would change
python3 install.py --apply    # create links, regenerate the Codex agents
python3 install.py --budget   # bytes loaded into every session
```

Sources are symlinked: edits to `AGENTS.md`, agents and skills take effect at the next session, but
`tools/run.py`, `tools/shell_guard.py` and `hooks/` run on every tool call, so a broken edit there is live
immediately (run the tests first). Keep this checkout on a branch that contains these files. An existing file
is replaced only if it is already a link or has identical content; otherwise it is reported as `CONFLICT`
and left untouched.

The Codex model of each agent is set in `CODEX_AGENTS` (`install.py`). Adding an agent also requires an
`[agents.<name>]` entry in `~/.codex/config.toml`. Claude skills listed in `CLAUDE_SKIP_SKILLS` are visible
only to Codex.

## Context rules

- Anything added to `AGENTS.md`, an agent description or a skill description is paid for in every session:
  check `install.py --budget` before and after.
- Procedures go in a skill (loaded on demand), not in `AGENTS.md`.
- Long output goes through `tools/run.py`: complete log on disk, bounded JSON in the context.

## Tests

```sh
python3 -m pytest tools
```

The tests use the deployed native binary (`~/.agents/tools/robot-exec/native/run`).
