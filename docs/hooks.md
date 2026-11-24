# Hooks

What each omega hook does, when it runs and what happens if it fails.

| Hook | Runs on | Effect | If the hook fails |
| --- | --- | --- | --- |
| [`shell_guard`](#shell_guard) | Bash calls (Claude, Codex, Vibe) | Wraps or denies shell commands | The command is **blocked** |
| [`context_guard`](#context_guard) | Read, Edit/Write, session start, compaction (Claude) | Bounds large reads, refuses re-reads | The call goes through |
| [`context_budget`](#context_budget) | Every tool call, prompt, compaction (Claude) | Checkpoint reminder, compaction instructions | The call goes through |
| `session_name` | Session start, prompt (Claude) | Names the session | The call goes through |
| `make_page.py --answered` / `--activity` | Prompt; session start, stop, notification, end (Claude) | Clears the asking session's pending question; records session activity for the progress hub (async) | The call goes through |

## `shell_guard`

- A plain command that the policy would allow once wrapped is **rewritten** to `Ω-run --shell`
  instead of being denied (Claude only). A miswritten wrapper call (`Ω-run …` with an outer pipe
  or chaining) is denied with its fix instead of being nested in another wrapper; the `omega` CLI
  is a plain command and is rewritten.
- File removal commands are denied, with a hint to use `trash-put`.
- `pkill -f PATTERN` is denied unless the pattern cannot match its own command line (`[n]ame`,
  `^anchor`): it would kill the calling shell and the wrapper, leaving no result.
- It carries `onFailure: block`: a crash or timeout blocks the command instead of letting it
  through, because it is a safety control.

## `context_guard`

- **Large evidence files** (over 8 KiB) are redirected to `Ω-inspect`.
- **Large text files** (over 8 KiB) read without a limit of 1..200 are not denied: the Read runs
  as 200 lines from the requested offset (`updatedInput`) and says which range was returned. That
  rewrite is its only `allow`; Claude Code still applies deny and ask rules after it. A Read that
  reaches the end of the file within 300 lines, in a file of at most 30000 bytes, runs as written
  (bounding it would cost a second round trip for a short tail). This is a
  line bound, not a byte or token guarantee. Images, PDFs and notebooks keep their native behavior.
- **Re-reads**: in the main session (not subagents), a Read of lines already read and unchanged
  since is denied (`already in context`) only when every requested line is covered; a partly covered
  range is allowed. A range is recorded when the Read succeeded (`PostToolUse`), never before it
  runs. Editing or writing the file, compaction and `/clear` reset this.
- **C/C++**: the first C/C++ edit of a session gets a one-time pointer to the `cpp-quality` skill.
- **Code Mode** (experimental, measured on Claude Code 2.1.330): hooks run on each call made by
  the code, under the tool's usual name with a `tool_use_id` starting with `repl_`; rewrites and
  hook messages apply. Their output goes to the script, not to the context, so such a Read runs as
  written (no bound, no evidence refusal, neither recorded nor refused as a re-read) and
  `shell_guard` applies its deny and ask policies to a nested shell command but does not wrap it: the
  script receives the real output, not `Ω-run`'s bounded summary. A wrapper call written by the
  code is checked as usual. The `REPL` result itself is capped by Claude Code at about 30000
  characters. `context_budget` skips the zero-usage virtual records these calls leave in the
  transcript.

## `context_budget`

- Never blocks a tool.
- Reminds once, at `OMEGA_CONTEXT_SOFT` (150k tokens), to update the task file.
- Before each automatic compaction (`PreCompact`) it records the context size in
  `~/.agents/state/budget/compact-floor.json`; for 14 days the reminder is capped at 69 % of it, so
  it fires before compaction whatever window the model really has.
- The same hook prints the compaction instructions (task file and next action, verified decisions,
  uncommitted files, evidence paths, failed approaches), except when you pass your own to `/compact`.
- After a compaction, `SessionStart` tells the session to resume from the task file.
- It never blocks a prompt, even after the prompt cache has expired: Claude Code's built-in
  `idleCompaction` (from 200k tokens by default, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` down to 100k;
  compacts about 55 min after the last request, on the 1 h TTL only) covers long idle sessions.

## Registration

Every hook is registered by `install.py` (`GUARD_HOOKS`, `CONTEXT_HOOKS`, `CODEX_HOOKS`, `VIBE_HOOK`), never by
hand:

- An entry running one of our binaries is rewritten in place to the canonical form; anything else
  is left alone.
- The Codex hook is only added when absent, because Codex keys hook trust by position.
- The Vibe hook (`omega-shell-guard` in `~/.vibe/hooks.toml`, `pre_tool` on `bash|process_start`,
  `strict`) runs `shell_guard --vibe`: replies use Vibe's protocol, unwrapped commands are
  auto-wrapped, and an ask policy denies because a Vibe hook cannot ask the user. An entry of that
  name in another form is a conflict.
- Restart Claude Code after first installing a hook; context already used is not reclaimed.
