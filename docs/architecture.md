# Architecture

How omega is organized and the rules that keep it small.

## Principles

- **Pay once, not every session.** Anything in `AGENTS.md`, an agent description or a skill
  description is loaded into every session. Check `python3 install.py --budget` before and after
  changing them.
- **Procedures live in skills**, loaded on demand, not in `AGENTS.md`.
- **Long output stays on disk.** `Ω-run` keeps the complete log as evidence and returns bounded JSON.
- **Work survives the context.** Substantial work keeps its state in a task file under
  `~/.agents/tasks/active/`, which `Ω-resume` and the post-compaction hook point back to.
- **Learn from verified failures.** The `retro` skill (`/retro`) turns failures verified in
  `omega optimize` reports and task files into one-line lessons in
  `skills/<skill>/references/lessons.md` (add, merge, prune; 20 lines per file), read only when
  that skill loads.

## Layout

What each source is for:

| Source | Role |
| --- | --- |
| `AGENTS.md` | Global instructions, loaded in every session |
| `prompts/codex.md` | Compact replacement for Codex's stock base prompt; final response only |
| `output-styles/final-only.md` | Final response only, preserving Claude's coding instructions |
| `agents/*.md` | The three workers |
| `skills/*/SKILL.md` | On-demand procedures; only the `Use when …` description is loaded until a task matches |
| `rust/` | Cargo crate `omega-tools`: evidence executor, [hooks](hooks.md), `omega` CLI |
| `systemd/` | Daily maintenance timers |
| `tools/`, `hooks/` | Tests and golden fixtures for the binaries |
| `docs/` | This documentation and measurement notes |
| `.github/workflows/` | `Tests` on every push, `Release` on every `v*` tag |

Where the installer puts it:

- `AGENTS.md` → `~/.agents/AGENTS.md`; `~/.claude/CLAUDE.md` imports it and
  `~/.codex/AGENTS.md` links to it.
- `prompts/codex.md` → `~/.codex/instructions.md` (link), selected by `model_instructions_file`.
- `output-styles/final-only.md` → `~/.claude/output-styles/final-only.md` (link), selected by
  `outputStyle`.
- `agents/*.md` → `~/.claude/agents/` (links) and `~/.codex/agents/*.toml` (generated).
- `skills/` → `~/.agents/skills/` (Codex scans it) and `~/.claude/skills/` (links).
- `rust/` → real 0700 files: `~/.agents/tools/robot-exec/native/run`,
  `~/.agents/tools/{shell_guard,context_guard,context_budget,optimize}` and
  `~/.claude/hooks/session_name`.
- Command links → `~/.local/bin/Ω-{run,inspect,prune,optimize}`, `~/.local/bin/omega`
  and the `omega-*` ASCII names.
- `systemd/` → linked by hand ([installation](installation.md#schedule-maintenance)).

Runtime state stays outside the repository: `~/.agents/tasks/` (`active/`, `completed/`,
`evidence/`, `reports/`, `state/`) and `~/.agents/state/`.

## Agents

| Agent | Use for | Can edit |
| --- | --- | --- |
| `scout` | Fast lookups, log triage | No |
| `coder` | A scoped change whose location and approach are known | Yes |
| `architect` | Design tradeoffs, unresolved root causes, risky interfaces | No |

Robotics is a skill domain (`robotics-learning`, `robotics-validation`, …), not a role. The Codex
model of each agent is set in `CODEX_AGENTS` (`install.py`), which generates standalone
`~/.codex/agents/<name>.toml` roles. Claude skills listed in `CLAUDE_SKIP_SKILLS` are visible only
to Codex.

### How "read-only" is actually enforced

- **Claude:** the `tools` allowlist removes `Edit`, `Write`, `NotebookEdit` and `Agent`
  (runtime-enforced). `Bash` is kept for `Ω-run`/`Ω-inspect`; it can still write under the parent's
  permission mode and allow rules (verified: an allowlisted `touch` succeeds even with
  `permissionMode: plan`), so no `permissionMode` is set. Bash read-only behavior is
  instruction-only.
- **Codex:** subagents inherit the parent turn's sandbox and approvals (including `/permissions`,
  `--yolo`); an agent file cannot narrow them, so no `sandbox_mode` is generated. Read-only is
  instruction-only unless the parent session itself runs `--sandbox read-only`.
