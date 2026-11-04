# omega

Versioned source of my Claude Code and Codex agent setup. The goal is to keep it small and limit
context rot: one short instruction file, a few agents, skills loaded on demand, and command output
kept on disk instead of in the context.

The previous Omega project (memory, code graph, dashboard) is archived under the `omega-legacy` tag.

## Layout

| Path | Deployed to | Role |
| --- | --- | --- |
| `AGENTS.md` | `~/.agents/AGENTS.md` (`~/.claude/CLAUDE.md` imports it, `~/.codex/AGENTS.md` links to it) | Global instructions, loaded in every session |
| `prompts/codex.md` | `~/.codex/instructions.md` (link), selected by `model_instructions_file` | Compact replacement for Codex's stock base prompt; final response only |
| `output-styles/final-only.md` | `~/.claude/output-styles/final-only.md` (link), selected by `outputStyle` | Final response only, preserving Claude's coding instructions |
| `agents/*.md` | `~/.claude/agents/` (link), `~/.codex/agents/*.toml` (generated) | The 4 workers: scout, coder, architect, robotics-rl |
| `skills/*/SKILL.md` | `~/.agents/skills/` (Codex scans it), `~/.claude/skills/` | Only the description is loaded until the skill is used |
| `rust/` | `~/.agents/tools/robot-exec/native/run`, `~/.agents/tools/robot-exec/local-read`, `~/.agents/tools/shell_guard`, `~/.claude/hooks/session_name` | Cargo crate `omega-tools`: evidence executor, trusted read launcher, PreToolUse guard (Claude + Codex), session naming hook. Built and installed as real 0700 files by `install.py --apply` |
| Command links | `~/.local/bin/Ω-{run,read,inspect,prune}` | Short names for the native executables; `omega-*` names are also installed |
| Context maintenance | `~/.local/bin/omega optimize`, `Ω-optimize` | Incremental Claude/Codex analysis, saved report and evidence-driven safe fixes; no transcript rewriting |
| `tools/`, `hooks/` | `~/.agents/tools/`, `~/.claude/hooks/` | Docs, tests and golden fixtures for those binaries (tests and fixtures are not linked) |
| `docs/` | — | Notes (backend audit) |

Runtime state stays outside the repository in `~/.agents/tasks/` (`active/`, `completed/`, `evidence/`).

## Install / update

```sh
python3 install.py            # dry run: lists what would change
python3 install.py --apply    # create links, regenerate the Codex agents, build and install the Rust binaries
python3 install.py --budget   # bytes loaded into every session
python3 install.py --responses-only --apply  # update only response prompts and their selection
python3 install.py --context-only --apply    # install context guard, audit CLI and command links only
```

Sources are symlinked: edits to `AGENTS.md`, agents and skills take effect at the next session.
Response settings are merged into the existing Codex TOML and Claude JSON without resetting
models, permissions or hooks. A different existing Codex `model_instructions_file` is reported
as a conflict. Start a new Codex conversation and restart Claude Code after installation;
existing conversations can retain their original prompts. Model caches may regenerate and
do not control these overrides. Tool displays and required approval prompts remain visible.
Rust binaries are only updated by `install.py --apply`, so run the tests first.

### Recurring context maintenance

Run `omega optimize` (or `Ω-optimize`) periodically: analyze, save a report, then apply
supported safe fixes. It selects up to 12 changed coding sessions across top-level Claude
transcripts (`~/.claude/projects/*/*.jsonl`) and recursive Codex transcripts
(`~/.codex/sessions/**/*.jsonl`), not a live-session inventory. `--sessions 1..50` changes
the changed-session budget; unchanged files do not consume it.

After the first baseline, only complete records appended since each session's last successful
optimization checkpoint are analyzed. Timestamp, byte cursor, prefix hash and file fingerprint
are stored in `~/.agents/tasks/state/omega-optimize.json`. Partial trailing records are deferred;
unfinished tool metadata is carried to the next window. Replaced, truncated or rewritten files
receive a full baseline, as do changed legacy entries without a cursor. `--force` explicitly
reanalyzes full sessions. Only successful runs advance checkpoints; files changing during an
audit remain eligible for the next run. A process lock and atomic replacement protect state.

Every run saves a private JSON report under `~/.agents/tasks/reports/omega-optimize/` before
advancing checkpoints. `--report NEW.json` selects another destination without overwriting;
`--json` also emits the report. Findings contain metrics, session/byte-range evidence references,
actions and fix statuses, not raw messages, commands or tool output. Review the report to make
targeted workflow/code improvements; unhandled findings remain explicitly marked for follow-up.

When new Claude findings show unbounded reads, the default enables the installed Read guard
if missing. `--check` disables automatic fixes; `--apply` explicitly enables the guard even
without new findings. Other hooks, disabled-hook preferences, permissions and configuration
symlinks are preserved. Generic source changes, compaction and fixes to live Codex context
require agent/user follow-up; the command never executes instructions from transcripts or
claims that a guard reclaims existing context. It never rewrites conversations.

The guard redirects evidence files over 8 KiB to Ω-inspect; other text over 8 KiB requires
an explicit Read limit of 1..200 lines. This is a line bound, not a byte/token guarantee.
Images, PDFs and notebooks retain native behavior. Restart Claude Code after first installing
the hook; existing conversation context is not reclaimed by the guard.

Claude input context is input + cache creation + cache read tokens, deduplicated by message
ID. Codex uses recorded `last_token_usage.input_tokens`; cached input is already included,
and cumulative token totals are not context size. Tool calls/results are deduplicated by tool-use ID. Large text reads and repeated
calls are review indicators, not proof of context rot; image serialization is excluded from
text-size indicators. Model window utilization and hardware validity are not inferred.
Keep this checkout on a branch that contains these files. An existing text file or command link
is replaced only if it is already a link or has identical content; otherwise it is reported as `CONFLICT`
and left untouched.

The Codex model of each agent is set in `CODEX_AGENTS` (`install.py`). Adding an agent also requires an
`[agents.<name>]` entry in `~/.codex/config.toml`. Claude skills listed in `CLAUDE_SKIP_SKILLS` are visible
only to Codex.

## Native commands

Keep `~/.local/bin` on `PATH`. Use the short commands directly, without repeating a `run` subcommand:

```sh
'Ω-run' --timeout 120 -- python3 -m pytest tools hooks
'Ω-run' --shell 'git status --short && git diff --stat'
'Ω-read' text AGENTS.md 1 20
'Ω-inspect' EVIDENCE --stream stderr --search error --lines 20
'Ω-prune' --days 14
```

`Ω-run` captures complete output in `~/.agents/tasks/evidence/` and returns bounded JSON;
`Ω-inspect` reads that evidence. `Ω-read` provides restricted source reads (`status`, `files`,
`text`, `find`). `Ω-prune` previews evidence cleanup; deletion requires `--apply` and approval.
Quote the `Ω-*` names as shown to match the shell guard's canonical `shlex.join` quoting.
The ASCII names (`omega-run`, `omega-read`, `omega-inspect`, `omega-prune`) and original absolute
paths remain supported. The guard checks command resolution on `PATH` and preserves native
sandbox and approval checks.

Oversized output includes a bounded `preview` (beginning/end and prioritized errors);
complete logs and existing failure `diagnostics` remain intact. `--inline-bytes 0`
disables successful previews. Shell sequences now fail fast with errexit/pipefail;
`--shell-keep-going` explicitly restores legacy last-command behavior for exploration,
not required validations. Bash inherits errexit in substitutions. Shell conditionals,
`||`, `!` and background jobs can still intentionally consume errors: run required
checks as standalone argv or unconditional steps and inspect their exit statuses.

For handoff/resume, `omega resume TASK` reads bounded task state, preferring active/
over completed/, without opening transcripts or logs; `omega resume` lists candidates.
Check current Git and the relevant referenced evidence before opening a transcript
for a specific missing detail. Task location alone does not prove completion.

`omega optimize` also reports returned-content overlap across different read commands
and ranges, with novel lines counted separately. Only the latest 2048 distinct line
digests per session survive checkpoints; no source text is persisted for this feature.
Lines shorter than 32 bytes, images and failed reads are excluded. Findings are advisory:
they neither suppress reads nor prove freshness or common source identity.
See [measurement method and results](docs/context-reliability.md).

See [native executor usage](tools/robot-exec/native/README.md) and
[trusted read capabilities](tools/robot-exec/README.md) for options and constraints.

## Context rules

- Anything added to `AGENTS.md`, an agent description or a skill description is paid for in every session:
  check `install.py --budget` before and after.
- Procedures go in a skill (loaded on demand), not in `AGENTS.md`.
- Long output goes through `Ω-run`: complete log on disk, bounded JSON in the context.

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
`tools/test_omega_commands.py` checks both command prefixes, installer links, dispatch and guard
policy; it defaults to the release binaries in `rust/target/release/` and accepts the same overrides.
`python3 -m pytest tools/test_response_settings.py` checks response setting preservation,
conflicts, idempotence, config symlinks and deployment without unrelated builds.
