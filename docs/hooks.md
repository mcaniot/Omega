# Hooks

What each omega hook does, when it runs and what happens if it fails.

| Hook | Runs on | Effect | If the hook fails |
| --- | --- | --- | --- |
| [`shell_guard`](#shell_guard) | Bash calls (Claude, Codex) | Wraps or denies shell commands | The command is **blocked** |
| [`context_guard`](#context_guard) | Read, Edit/Write, session start, compaction (Claude) | Bounds large reads, refuses re-reads | The call goes through |
| [`context_budget`](#context_budget) | Every tool call, prompt, compaction (Claude) | Checkpoint reminder, compaction instructions | The call goes through |
| `session_name` | Session start, prompt (Claude) | Names the session | The call goes through |

## `shell_guard`

- A plain command that the policy would allow once wrapped is **rewritten** to `Ω-run --shell`
  instead of being denied (Claude only).
- File removal commands are denied, with a hint to use `trash-put`.
- It carries `onFailure: block`: a crash or timeout blocks the command instead of letting it
  through, because it is a safety control.

## `context_guard`

- **Large evidence files** (over 8 KiB) are redirected to `Ω-inspect`.
- **Large text files** (over 8 KiB) read without a limit of 1..200 are not denied: the Read runs
  as 200 lines from the requested offset (`updatedInput`) and says which range was returned. That
  rewrite is its only `allow`; Claude Code still applies deny and ask rules after it. This is a
  line bound, not a byte or token guarantee. Images, PDFs and notebooks keep their native behavior.
- **Re-reads**: in the main session (not subagents), a Read of lines already read and unchanged
  since is denied (`already in context`); a partly covered range is narrowed to the missing lines.
  Editing or writing the file, compaction and `/clear` reset this.
- **C/C++**: the first C/C++ edit of a session gets a one-time pointer to the `cpp-quality` skill.

## `context_budget`

- Never blocks a tool.
- Reminds once, at `OMEGA_CONTEXT_SOFT` (150k tokens), to update the task file.
- Before each automatic compaction (`PreCompact`) it records the context size in
  `~/.agents/state/budget/compact-floor.json`; for 14 days the reminder is capped at 69 % of it, so
  it fires before compaction whatever window the model really has.
- The same hook prints the compaction instructions (task file and next action, verified decisions,
  uncommitted files, evidence paths, failed approaches), except when you pass your own to `/compact`.
- After a compaction, `SessionStart` tells the session to resume from the task file.

## Registration

Every hook is registered by `install.py` (`GUARD_HOOKS`, `CONTEXT_HOOKS`, `CODEX_HOOKS`), never by
hand:

- An entry running one of our binaries is rewritten in place to the canonical form; anything else
  is left alone.
- The Codex hook is only added when absent, because Codex keys hook trust by position.
- Restart Claude Code after first installing a hook; context already used is not reclaimed.
