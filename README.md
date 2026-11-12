<h1 align="center">
  <img src="assets/icon-round.png" alt="" width="120" align="absmiddle">
  omega
</h1>

Versioned source of my Claude Code and Codex agent setup. The goal is to keep it small and limit
context rot: one short instruction file, a few agents, skills loaded on demand, and command output
kept on disk instead of in the context.

The previous Omega project (memory, code graph, dashboard) is archived under the `omega-legacy` tag.
Releases and their changes are listed in [CHANGELOG.md](CHANGELOG.md); `omega --version` prints the
installed version.

- [Layout](#layout)
- [Install and update](#install-and-update)
- [Native commands](#native-commands)
- [Hooks](#hooks)
- [`omega optimize`](#omega-optimize)
- [Context rules](#context-rules)
- [Versioning and release](#versioning-and-release)
- [Tests](#tests)

## Layout

| Path | Deployed to | Role |
| --- | --- | --- |
| `AGENTS.md` | `~/.agents/AGENTS.md` (`~/.claude/CLAUDE.md` imports it, `~/.codex/AGENTS.md` links to it) | Global instructions, loaded in every session |
| `prompts/codex.md` | `~/.codex/instructions.md` (link), selected by `model_instructions_file` | Compact replacement for Codex's stock base prompt; final response only |
| `output-styles/final-only.md` | `~/.claude/output-styles/final-only.md` (link), selected by `outputStyle` | Final response only, preserving Claude's coding instructions |
| `agents/*.md` | `~/.claude/agents/` (link), `~/.codex/agents/*.toml` (generated standalone roles) | The 3 workers: scout, coder, architect; robotics is a skill domain (`robotics-learning`, …), not a role |
| `skills/*/SKILL.md` | `~/.agents/skills/` (Codex scans it), `~/.claude/skills/` | Only the description (`Use when …` triggers) is loaded until the task matches; agents never preload skills |
| `rust/` | `~/.agents/tools/robot-exec/native/run`, `~/.agents/tools/robot-exec/local-read`, `~/.agents/tools/{shell_guard,context_guard,context_budget,optimize}`, `~/.claude/hooks/session_name` | Cargo crate `omega-tools`: evidence executor, trusted read launcher, hooks, `omega` CLI. Built and installed as real 0700 files by `install.py --apply` |
| Command links | `~/.local/bin/Ω-{run,read,inspect,prune,optimize}`, `~/.local/bin/omega` | Short names for the native executables; `omega-*` names are also installed |
| `systemd/` | linked by hand (see below) | Daily user timers for `omega optimize` and evidence pruning |
| `tools/`, `hooks/` | — | Docs, tests and golden fixtures for the binaries |
| `docs/` | — | Notes: [context reliability measurements](docs/context-reliability.md), [native backend audit](docs/native-audit.md) |
| `.github/workflows/` | — | `Tests` on every push, `Release` on every `v*` tag |
| `CHANGELOG.md`, `assets/` | — | Release notes, icon |

Runtime state stays outside the repository in `~/.agents/tasks/` (`active/`, `completed/`,
`evidence/`, `reports/`, `state/`) and `~/.agents/state/`.

## Install and update

```sh
python3 install.py            # dry run: lists what would change
python3 install.py --apply    # create links, regenerate the Codex agents, build and install the Rust binaries
python3 install.py --budget   # bytes loaded into every session
python3 install.py --responses-only --apply  # update only response prompts and their selection
python3 install.py --context-only --apply    # install context guard, audit CLI and command links only
```

Keep this checkout on a branch that contains these files. Sources are symlinked: edits to
`AGENTS.md`, agents and skills take effect at the next session. Rust binaries are only updated by
`install.py --apply`, so run the [tests](#tests) first. Start a new Codex conversation and restart
Claude Code after installation; existing conversations can retain their original prompts.

What the installer preserves:

- An existing text file or command link is replaced only if it already links into this repository
  or has identical content; otherwise it is reported as `CONFLICT` and left untouched.
- Response settings are merged into the existing Codex TOML and Claude JSON without resetting
  models, permissions or hooks. A different existing Codex `model_instructions_file` is reported as
  a conflict. Model caches may regenerate and do not control these overrides. Tool displays and
  required approval prompts remain visible.
- Hooks: see [Hooks](#hooks).

Agents:

- The Codex model of each agent is set in `CODEX_AGENTS` (`install.py`); `install.py --apply`
  generates standalone `~/.codex/agents/<name>.toml` roles, no `[agents.<name>]` entry is needed.
  An existing role file is rewritten only if it has the exact shape the installer writes;
  otherwise it is a `CONFLICT`.
- Only retired Omega roles listed in `RETIRED_AGENTS`, their legacy `[agents.*]` registrations and
  dangling links are removed; other agent files, TOML and configuration are never deleted.
- Claude skills listed in `CLAUDE_SKIP_SKILLS` are visible only to Codex.

Read-only roles (`scout`, `architect`) — actual enforcement:

- Claude: the `tools` allowlist removes `Edit`, `Write`, `NotebookEdit` and `Agent` (runtime-enforced).
  `Bash` is kept for `Ω-run`/`Ω-inspect`; it can still write under the parent's permission mode and
  allow rules (verified: an allowlisted `touch` succeeds even with `permissionMode: plan`), so no
  `permissionMode` is set. Bash read-only behavior is instruction-only.
- Codex: subagents inherit the parent turn's sandbox and approvals (including `/permissions`, `--yolo`);
  an agent file cannot narrow them, so no `sandbox_mode` is generated. Read-only is instruction-only
  unless the parent session itself runs `--sandbox read-only`.

### Recurring maintenance

User systemd timers run [`omega optimize`](#omega-optimize) and the evidence cleanup
(`omega-prune --apply`, 14 days) daily:

```sh
systemctl --user link "$PWD"/systemd/omega-{optimize,prune}.{service,timer}
systemctl --user enable --now omega-optimize.timer omega-prune.timer
```

## Native commands

Keep `~/.local/bin` on `PATH`. Use the short commands directly, without repeating a `run` subcommand:

```sh
'Ω-run' --timeout 120 -- python3 -m pytest tools hooks
'Ω-run' --shell 'git status --short && git diff --stat'
'Ω-read' text AGENTS.md 1 20
'Ω-inspect' EVIDENCE --stream stderr --search error --lines 20
'Ω-prune' --days 14
omega resume TASK
omega --version
```

- `Ω-run` captures complete output in `~/.agents/tasks/evidence/` and returns bounded JSON;
  `Ω-inspect` reads that evidence.
- `Ω-read` provides restricted source reads (`status`, `files`, `text`, `find`) under the single
  project root compiled into it (`LOCAL_READ_PROJECT`); it is not advertised in `AGENTS.md` or the
  agents, which read files with their native Read tool.
- `Ω-prune` previews cleanup of evidence older than `--days` (default 14) that no active or
  completed task cites; deletion requires `--apply` and approval.
- `omega resume TASK` reads bounded task state, preferring `active/` over `completed/`, without
  opening transcripts or logs; `omega resume` lists the 20 most recently modified candidates across
  both directories (ties: state, then name). Check current Git and the relevant referenced evidence
  before opening a transcript for a specific missing detail. Task location alone does not prove
  completion.

Quote the `Ω-*` names as shown to match the shell guard's canonical `shlex.join` quoting.
The ASCII names (`omega-run`, `omega-read`, `omega-inspect`, `omega-prune`, `omega-optimize`) and
original absolute paths remain supported. The guard checks command resolution on `PATH` and
preserves native sandbox and approval checks.

Output and exit status:

- Oversized output includes a bounded `preview` (beginning/end and prioritized errors); complete
  logs and existing failure `diagnostics` remain intact. `--inline-bytes 0` disables successful
  previews.
- Shell sequences fail fast with errexit/pipefail; Bash inherits errexit in substitutions.
  `--shell-keep-going` explicitly restores legacy last-command behavior for exploration, not
  required validations. Shell conditionals, `||`, `!` and background jobs can still intentionally
  consume errors: run required checks as standalone argv or unconditional steps and inspect their
  exit statuses.
- For required checks, use `omega-run --validation -- COMMAND ARGS` once per check. This rejects
  `--shell` (including keep-going mode) before execution; it does not inspect arbitrary
  executables or scripts. PASS only means that child exited zero.

See [native executor usage](tools/robot-exec/native/README.md) and
[trusted read capabilities](tools/robot-exec/README.md) for options and constraints.

## Hooks

Every Omega hook is registered by `install.py` (`GUARD_HOOKS`, `CONTEXT_HOOKS`, `CODEX_HOOKS`),
never by hand: an entry running one of our binaries is rewritten in place to the canonical form,
anything else is left alone. The Codex hook is only added when absent, because Codex keys hook
trust by position. Restart Claude Code after first installing a hook; existing conversation
context is not reclaimed.

- `shell_guard` (Claude and Codex, `PreToolUse` Bash): an unwrapped command that the policy would
  allow once wrapped is rewritten to `Ω-run --shell` instead of being denied (Claude only); file
  removal commands are denied with a `trash-put` hint. It carries `onFailure: block`, so a crash or
  timeout blocks the command instead of letting it through.
- `context_guard` (Claude, fail-open):
  - Evidence files over 8 KiB are redirected to `Ω-inspect`.
  - A Read of other text over 8 KiB without a limit of 1..200 is not denied: it runs as 200 lines
    from the requested offset (`updatedInput`) and says which range was returned. That rewrite is
    its only `allow`; Claude Code still applies deny and ask rules after it. This is a line bound,
    not a byte/token guarantee. Images, PDFs and notebooks retain native behavior.
  - In the main session (not subagents), a Read of lines already read and unchanged since is
    denied (`already in context`), and a partly covered range is narrowed to the missing lines.
    Edit/Write of a file, compaction and `/clear` reset that ledger.
  - The first C/C++ edit of a session gets a one-time pointer to `cpp-quality`.
- `context_budget` (Claude, fail-open): never blocks a tool. It reminds once, at
  `OMEGA_CONTEXT_SOFT` (150k), to update the task file. Its `PreCompact` hook records the context
  size of each automatic compaction in `~/.agents/state/budget/compact-floor.json`; for 14 days the
  reminder is capped at 69 % of it, so it fires before compaction whatever window the model really
  has. The same hook prints the compaction instructions (task file and next action, verified
  decisions, uncommitted files, evidence paths, failed approaches), except when the user passed
  their own to `/compact`. After a compaction, `SessionStart` tells the session to resume from the
  task file.
- `session_name` (Claude, `SessionStart`/`UserPromptSubmit`): names the session.

## `omega optimize`

`omega optimize` (or `Ω-optimize`) analyzes recent transcripts, saves a report, then applies
supported safe fixes. It never rewrites, deletes or compacts conversations and never executes
instructions from transcripts.

```sh
omega optimize [--sessions 1..50] [--json] [--report NEW.json] [--apply | --check] [--force] [--compare BASE.json]
omega optimize prefix [--json] [--report NEW.json]   # read-only fixed-context cost per project
omega optimize replay [--sessions 1..50] [--json]    # read-only counterfactual token replay
```

Selection and checkpoints:

- It selects up to 12 changed coding sessions across top-level Claude transcripts
  (`~/.claude/projects/*/*.jsonl`) and recursive Codex transcripts (`~/.codex/sessions/**/*.jsonl`),
  not a live-session inventory. `--sessions 1..50` changes the changed-session budget; unchanged
  files do not consume it.
- After the first baseline, only complete records appended since each session's last successful
  checkpoint are analyzed. Timestamp, byte cursor, prefix hash and file fingerprint are stored in
  `~/.agents/tasks/state/omega-optimize.json`. Partial trailing records are deferred; unfinished
  tool metadata is carried to the next window. Replaced, truncated or rewritten files receive a
  full baseline, as do changed legacy entries without a cursor. `--force` reanalyzes full sessions.
- Only successful runs advance checkpoints; files changing during an audit remain eligible for the
  next run. A process lock and atomic replacement protect state.

Reports and fixes:

- Every run saves a private JSON report under `~/.agents/tasks/reports/omega-optimize/` before
  advancing checkpoints. `--report NEW.json` selects another destination without overwriting;
  `--json` also emits the report. Findings contain metrics, session/byte-range evidence references,
  actions and fix statuses, not raw messages, commands or tool output. Review the report to make
  targeted workflow/code improvements; unhandled findings remain explicitly marked for follow-up.
- When new Claude findings show unbounded reads, the default enables the installed Read guard if
  missing. `--check` disables automatic fixes; `--apply` explicitly enables the guard even without
  new findings. Other hooks, disabled-hook preferences, permissions and configuration symlinks are
  preserved. Generic source changes, compaction and fixes to live Codex context require agent/user
  follow-up.
- `--compare BASE.json` prints OK/KO gates against a base report produced by this version (rerun
  the base sessions with `--force`); the exit status stays 0.

Metrics:

- Claude input context is input + cache creation + cache read tokens, deduplicated by message ID.
  Codex uses recorded `last_token_usage.input_tokens`; cached input is already included, and
  cumulative token totals are not context size. Tool calls/results are deduplicated by tool-use ID.
- `tool_calls_per_request` is reported per session (about 1.0 means no batching).
- Returned-content overlap across different read commands and ranges is reported, with novel lines
  counted separately. Only the latest 2048 distinct line digests per session survive checkpoints;
  no source text is persisted. Lines shorter than 32 bytes, images and failed reads are excluded.
- Large text reads, repeated calls and overlap are review indicators, not proof of context rot or
  freshness; image serialization is excluded from text-size indicators. Model window utilization
  and hardware validity are not inferred.

See [measurement method and results](docs/context-reliability.md).

## Context rules

- Anything added to `AGENTS.md`, an agent description or a skill description is paid for in every
  session: check `install.py --budget` before and after.
- Procedures go in a skill (loaded on demand), not in `AGENTS.md`.
- Long output goes through `Ω-run`: complete log on disk, bounded JSON in the context.
- Self-improvement: the `retro` skill (`/retro`) turns failures verified in `omega optimize`
  reports and task files into one-line lessons in `skills/<skill>/references/lessons.md` (add,
  merge, prune; 20 lines per file), read only when that skill loads.

## Versioning and release

Versions follow [Semantic Versioning](https://semver.org/). The `version` in `rust/Cargo.toml` is
the single source: `omega --version` prints it, and `tools/test_version.py` checks that
`rust/Cargo.lock` and the latest `CHANGELOG.md` release agree with it.

To release:

1. Move the `[Unreleased]` entries of `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` and update the
   compare links.
2. Set `version = "X.Y.Z"` in `rust/Cargo.toml`, then run `cargo build --release --manifest-path
   rust/Cargo.toml` once (without `--locked`) to update `rust/Cargo.lock`.
3. Run the [tests](#tests), commit, then tag and push:
   `git tag -a vX.Y.Z -m "omega vX.Y.Z" && git push origin main vX.Y.Z`.
4. The `Release` workflow checks that the tag matches `rust/Cargo.toml` and has a changelog entry,
   then creates the GitHub Release with that entry as notes.
5. Deploy on each machine with `python3 install.py --apply`.

## Tests

The GitHub Actions `Tests` workflow builds fresh binaries and runs Rust/Python checks as
independent steps on push, pull request, or manual dispatch.

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
python3 -m pytest tools hooks
```

Tests drive the binaries and replay golden fixtures recorded from the original implementations:

| Test | Binary under test (override) |
| --- | --- |
| `tools/test_run.py`, `tools/robot-exec/native/test_{boundary,golden}.py` | deployed `run` (`OMEGA_RUN_BINARY`) |
| `tools/test_shell_guard.py` | deployed `shell_guard` (`OMEGA_SHELL_GUARD_BINARY`) |
| `hooks/test_session_name.py` | deployed `session_name` (`OMEGA_SESSION_NAME_BINARY`) |
| `tools/test_context_guard.py` | `rust/target/release/context_guard` (`OMEGA_CONTEXT_GUARD_BINARY`) |
| `tools/test_context_budget.py` | `rust/target/release/context_budget` (`OMEGA_CONTEXT_BUDGET_BINARY`) |
| `tools/test_optimize.py`, `tools/test_version.py` | `rust/target/release/optimize` (`OMEGA_OPTIMIZE_BINARY`) |
| `tools/test_omega_commands.py` | release binaries; both command prefixes, installer links, dispatch and guard policy |
| `tools/robot-exec/test_local_read.py` | builds its own binary with throwaway roots |
| `tools/test_response_settings.py` | installer only: response settings preservation, conflicts, idempotence, config symlinks |

To test a fresh build before installing, point the overrides at `rust/target/release/<name>`.
