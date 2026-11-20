# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/):
the version in `rust/Cargo.toml` is the single source, `omega --version` prints it, and each
release is the annotated Git tag `vX.Y.Z` (see [Release](docs/development.md#release)).

## [Unreleased]

### Added

- `review-loop` skill: review-fix loop on the user's own code. Each round a fresh read-only `Plan`
  agent on Fable reports bugs, line reductions and readability fixes with `file:line`, confidence
  and patch; the main session verifies, applies (bugs first), runs formatter, lint and tests, and
  measures net lines removed. Stops with no bug and under 5 lines gained, after 4 rounds, or on a
  recurring finding.
- `progress-page`: the hub closes and cleans missions by itself. A mission is finished when its task
  file moves to `completed/`; every step done with the task still active for a day shows "À clôturer";
  finished missions leave the hub after 7 days and unreadable states after 14 (files are kept).
- `progress-page`: Agents section in the hub, one row per session seen in 24 h with its state
  (travaille, attend ta réponse, à son invite, fermée, perdue), a 24 h timeline and its missions.
  Async hooks (`make_page.py --activity` on SessionStart, Stop, StopFailure, Notification and
  SessionEnd; `--answered` also records UserPromptSubmit) append to `~/.agents/activity/*.jsonl`
  (no prompt text, 7 days kept); liveness comes from Claude Code's session registry.
- `progress-page`: result tables. A section takes `table: {columns, rows}` beside or instead of
  `items`; `--section 'TITLE::JSON'` adds or replaces one by title. The skill puts the requested
  result (table, verdict, artifact paths) on the page before the final answer.
- `shell_guard` asks before `git stash` (except `list` and `show`).

### Fixed

- Whole-repository review (4 rounds). `shell_guard`: the command after wrapper options, every
  `--shell`/`--shell=` value, the text after `--` and `--native-only` are all checked; a `git` mask
  no longer hides a following `rm` (`git --no-pager log; rm f`); filters piped after `Ω-inspect`
  may not name a file, read `-f FILE` or recurse, and `--pre`/`--hostname-bin` are refused; nested
  or abbreviated `prune --apply` asks; an invalid policy regex aborts instead of passing; the
  evidence-read rewrite keeps every operand and expands `~`. `run`: `inspect --search` chunking,
  multibyte text and `$` anchors; `prune` keeps live runs; invalid UTF-8 keeps `result.json` valid;
  `nan`/`inf` timeouts refused; `|&`, a trailing separator and a newline after `|`/`&&` parse as
  bash does; exit 141 is no longer a PASS when an earlier pipeline (`head`, `grep -q`, `grep -m`)
  may have stopped the script. `optimize`: no report left behind on failure, buffered JSON
  writes, aligned tables, UTF-8 prefixes, `NotebookEdit`. `install.py`: dotfile differences and
  build errors are conflicts, hooks are not written over a conflict, atomic writes.
  `make_page.py`, `context_guard`, `context_budget`, `session_name` and `benchmark_context.py`:
  edge cases that crashed or misreported.

- `progress-page`: a question no longer stays "À toi" after the user answers. `--ask` records the
  Claude Code session, and a `UserPromptSubmit` hook (`make_page.py --answered`, registered by
  `install.py`) clears it on that session's next prompt, logs it and re-renders the page and hub.
  Without a session (Codex), the next `--step`, `--add-step`, `--fig`, `--log` or `--warn` update
  clears it. Waiting for the user (decision, authorization) uses `--ask`, never the `bloqué` step
  status, which nothing clears on the answer.
- `progress-page`: images show when only the HTML reaches the viewer (Orca remote connection, copied
  file). Images up to 2 MB, the logo and hub thumbnails up to 500 KB are embedded as `data:` URIs;
  larger files and videos stay links. Hub thumbnails are reduced JPEGs when Pillow is installed.
  Links between pages and the hub are relative instead of `file://`.
- `progress-page`: `omega-progress [HOST:]PORT` (`make_page.py --serve`) serves the hub and every
  registered page read-only over HTTP, behind a random token, for viewers without `file://` access
  (remote device). Default `127.0.0.1:8765`; only the hub and registered page directories are served.
  Off loopback it publishes `http://omega.local:PORT` on the local network (mDNS via avahi, `--name`);
  `--open` drops the token. Optional user service `systemd/omega-progress.service` keeps it running.
  Served pages load their images over HTTP instead of the embedded copies, and byte ranges are
  honoured so videos play in Safari and iOS.
  Hardened after review: only each page's `index.html` and its figure files are served (no state,
  lock, registry or hidden files, nothing outside the page directory); requests naming another host
  get 421 (DNS rebinding); files and byte ranges are streamed, not read whole; non-HTML files carry
  `ETag`/`Last-Modified` and answer 304; idle connections time out and malformed requests get 400;
  the server exits when the mDNS publisher dies, republishes on a new address, stops it on SIGTERM;
  the user unit is capped at 512 MB.
- `progress-page`: a finished page whose task file is active again (follow-up task linked with
  `--task`) shows "En cours" on the hub ("Reprise") instead of "Terminée". `--task` alone on a finished
  page is refused: the follow-up adds its steps with `--add-step` in the same command, or creates a
  new page.

## [1.1.0] - 2026-11-18

### Added

- `Ω-resume` (and `omega-resume`) runs `omega resume`; agents now call `'Ω-resume'` and
  `'Ω-optimize'`. The `omega resume` / `omega optimize` names keep working.
- One-command token benchmark (`tools/benchmark_context.py`) and its [Benchmark](docs/benchmark.md) page.
- Optional Omega theme for the Claude desktop app: `install.py --omega-theme --apply` links the
  plugin `themes/omega-theme/` (colours and an animated backdrop); without the flag nothing changes.
- `code-review` skill for MR/PR and local diffs: findings backed by `file:line`, scenario and
  consequence, checks labelled run / inspected / not verified, comments in Conventional Comments
  format proposed (blocking, then optional) and published inline with `glab` or `gh` only after explicit approval.
- `progress-page` skill: a static progress page (`index.html`) rendered from `state.json` by
  `scripts/make_page.py`, opened from `file://`. Text and media URLs escaped, malformed state
  rejected with the faulty key, PNG sizes reserved, pausable auto-reload, recent-changes log,
  atomic writes. Every page registers in one hub, `~/.agents/progress/index.html`, so all tracked
  tasks are followed from a single tab; a broken or corrupt entry never blocks the others.
  The hub ranks tasks waiting for the user (`ask`) first and counts them in the tab title, shows
  relative ages, flags silent tasks and updates since the last visit, and keeps unmounted pages
  listed. `--step`, `--add-step`, `--log`, `--fig`, `--warn` and `--ask` update `state.json` under
  a lock and render in one command; `--show` prints the numbered steps, `--all` re-renders every
  page, `--forget` removes one. Missions expected to run more than about an hour unattended create
  their page unprompted (`AGENTS.md`).
  Pages follow handoffs and `/clear`: `--task` and `--agent` link the page to its task file and
  session, a new agent is logged as a takeover and shown on the hub, and `--find TASK` returns the
  existing page so a resumed session never starts a second one.
  Redesign after a UX study (Linear, Geist, Primer, Carbon, NN/g): light and dark themes from the
  Omega palette, solid orange reserved for questions waiting for the user, status icons with
  distinct shapes, per-step progress bars, hub grouped by urgency with summary chips, task page
  with a journal sidebar and an in-page image viewer.

### Changed

- Robotics skills, from a review of DeepMind's robotics-antigravity-plugin: `systematic-debugging` adds the blind
  video second opinion (a fresh worker sees only the failure video before any code) and exporting the compiled model of
  Newton's MuJoCo solver before a contact hypothesis; `robotics-learning` forbids improvement claims from one seed or
  unpaired runs (paired McNemar test); `robotics-validation` pairs candidate and backend comparisons on identical inputs.
- `coder` gets one bounded step per run (about 5 files; larger plan steps are split) with the known
  `file:line` map in its brief, returns `status: partial` when the work grows beyond that, and
  stops at `maxTurns: 120`. Recorded runs of 60+ requests (whole plan steps, ~11 files, contexts
  near 200k, 19 of 36 compacted) were 72 % of coder cost; the median run is 42 requests.
- End-to-end A/B of the current version against plain Claude Code (advisor off, 3 repetitions,
  27/27 both): −24 % cost, −49 % turns, −33 % wall time; README and benchmark page updated.
  `tools/benchmark_context.py` passes `--fail-fast` for the strict-shell scenario (it failed on
  every version since strict shell became opt-in).
- The benchmark page separates tool-output savings (−86 %, synthetic, no model call) from
  end-to-end results (headless A/B and recorded sessions); the README no longer presents the
  tool-output figure alone.
- AGENTS.md no longer tells Claude to run every command through `Ω-run`: `shell_guard
  --allow-clean` already wraps clean commands, and its notice no longer asks for the wrapper. On a
  9-task headless A/B (3 reps each, all 27/27) the session cost dropped from 0.293 to 0.265 USD
  and wall time from 56 to 38 s. Codex, whose guard denies unwrapped commands, keeps the rule in
  `prompts/codex.md`. The coder/architect/scout agents follow: the rule is appended only to their
  Codex TOML.
- Cold resume names the session's task (`'Ω-resume' <task>`, newest existing active task file
  mentioned in the transcript) and does not block a session without one.
- Cold resume no longer echoes the prompt (Claude Code appends it under `Original prompt:`) and
  stays silent outside the interactive CLI (`CLAUDE_CODE_ENTRYPOINT` other than `cli`), where a
  block would end a `claude -p --resume` or SDK run unanswered. The `Ω-read` rewrite leaves `~`
  paths, globs, comments and multi-line commands to the deny guidance.
- `shell_guard` runs a call of the retired `Ω-read FILE…` (39 failed calls recorded since
  2026-11-09) as `'Ω-run' --shell 'head -n 200 -- FILE…'` when every argument is a plain path.
- `context_budget` blocks, once per idle period, the first prompt after more than 1 h idle with at
  least 60k tokens of context: the prompt cache has expired and the prompt would rewrite the whole
  context at the cache-write price. The notice echoes the prompt and suggests `/clear` then
  `'Ω-resume'`; resending continues. Cost-weighted replay of 69 recorded Opus sessions: −13 to
  −15 %. A hard context cap was not adopted (−7 to −16 %, but 131–193 restarts).
- Delegation follows Anthropic's subagent guidance (code.claude.com/docs/en/sub-agents, costs page,
  multi-agent research system): only self-contained work whose raw output the main session will not
  reuse is delegated; coupled plan/implement/test stays in the main session. `coder` is for
  self-contained changes, verbose validation or independent parallel changes; `Plan` for prior art,
  read-heavy investigations and clean-context diff review; worker replies stay under about 1,500
  tokens. Recorded sessions: subagents went from 0.35 to 3.33 USD per Opus session while the main
  session cost did not drop.
- `shell_guard` auto-wraps a heredoc whose body holds an apostrophe (the body is data; only the
  lines around it must parse), and runs a `'Ω-run' [OPTIONS] --shell '…'` call whose script holds
  apostrophes (`s.replace('a', 'b')` in a heredoc) as the text between its outer quotes, unless
  the apostrophes are escaped (`'"'"'`), separate arguments or chain another wrapper call. The
  policy still reads the whole text. Replayed on the 670 denials recorded since 2026-11-01: 99
  fewer denials (344 → 245), each a saved round trip; of the 296 scripts the guard now runs for them, 295 pass `bash -n`.
- `shell_guard` auto-fixes a wrapper call with something outside it: an outer redirection or
  filter moves into the script (`'Ω-run' --shell 'X' 2>&1 | head -5` →
  `'Ω-run' --shell 'X 2>&1 | head -5'`, `… > FILE` likewise), a trailing `; true`, `|| true` or
  `; echo` is dropped, and a plain command after the call gets its own wrapper call. Not when the
  first command is not a wrapper call, a later plain command changes shell state (`cd`, `X=`), or a
  job runs in the background; the policy checks the result. Replayed: 26 fewer denials (245 → 219),
  all 26 scripts pass `bash -n`.
- `shell_guard` requotes an argv wrapper call whose meaning is unambiguous but whose quoting the
  shell would not take: `!` or `\"` inside double quotes (`rg "(?!x)" -g "!a/**"`), `(`, `)`, `^`
  or a backslash escape outside quotes (`rg -F -e self.reset()`, `sed -n 1p\;5p`). Expansions a
  shell would make and shlex would not (`$`, backquote, `~`, braces, `#`, `\$`, globs except as a
  `-g`/`--glob`/`--include` pattern) stay denied. Replayed: 39 fewer denials (219 → 180).
- `shell_guard` allows `cut` after `Ω-inspect`, several read-only filters in a row
  (`| cut -c1-700 | head -n 45`) and `2>/dev/null` before them: 5 fewer denials (180 → 175).
- `shell_guard` moves a heredoc that feeds an argv wrapper call
  (`'Ω-run' -- python3 - <<'EOF'` … `EOF`) into `--shell` with the command: the wrapper passes no
  stdin on, so the outer heredoc fed nothing. Only one body closed on the last line. Replayed: 10
  fewer denials (175 → 165).
- `shell_guard` runs a `--shell` script that escapes its apostrophes (`<<'"'"'EOF'"'"'`) but left a
  raw one inside a heredoc body (`the episode's end`) as the decoded script: the body is data.
  A raw apostrophe outside a body or an unclosed body still denies. Replayed: 11 fewer denials
  (165 → 154).
- `shell_guard`: options written after a `--shell` script (`--shell '…' --cwd DIR`) move before
  it; a chain may start with an `Ω-inspect` call; a leading call with options and no command
  (`'Ω-run' --shell-keep-going 2>/dev/null; CALL`, which only fails) is dropped; `Ω-resume` and
  `Ω-optimize` take the same read-only filters as `Ω-inspect`; a quoted argument starting with `=`
  (`--search "=="`) is no longer denied as a zsh EQUALS expansion; `--shell-executable PATH` no
  longer stops a literal script. Replayed: 18 fewer denials (154 → 136).
- A clean `Ω-run` `PASS` (exit 0, no note) whose output fits inline prints that output as is,
  stdout then stderr, like the plain Bash tool, instead of a JSON envelope: in recorded sessions
  93 % of results were such runs and the envelope cost about 190 bytes each (0.7 M tokens over
  two weeks). Failures, `NO_MATCH`, notes, truncated output, invalid UTF-8 and NUL bytes keep the
  JSON; `OMEGA_RUN_JSON=1` forces it.
- `Ω-run` returns up to 30000 bytes of output inline by default (was 8192; parity with the plain
  Bash tool, so a medium output no longer costs an `Ω-inspect` round trip); the JSON budget is
  40 KiB. Failure `diagnostics` now also surface link errors (`undefined reference`, `ld returned`,
  `cannot find -l`), `Killed`/out-of-memory, `nan`/`inf` values, timeouts, `Permission denied`,
  `No such file or directory`, `command not found`, `CONFLICT (` and rustc `error[E0308]`; zero
  counts such as `0 failed` are not failures; near-identical lines (digit runs collapsed) share one
  slot, so a repeated retry line no longer hides a different error. Warning lines fill the slots
  that errors leave free (a failure whose only trace is a warning used to show nothing useful).
- `Ω-inspect --search` is a regex with smart case (literal when it is not a valid regex) and returns
  whole matching lines; paging defaults to 100 lines / 16000 bytes, with caps of 200 lines /
  30000 bytes (was 20 / 4096, caps 40 / 7000).
- Failure `diagnostics` rank first the cause and the verdict: an exception at line start (also a
  warning raised as an error), `___ test_name ___` failure headers (pytest, sympy `bin/test`) and a
  nonzero test-summary count (`52 exceptions`, `3 failed`). Traceback frames and echoed source only
  fill the remaining slots (a SWE-bench run used to need three follow-up searches).
- In Claude Code, `scout` and `architect` are named `Explore` and `Plan`: a same-named user agent
  overrides the built-in, so plan mode and the model's habit of calling `Explore` reach the Haiku
  `scout` instead of an Opus subagent (25 Opus `Explore` runs at 1.84 USD each in two weeks).
  Codex names are unchanged. `install.py` denies the built-in `general-purpose`, which duplicates
  `coder`. `scout` no longer loads skills; its turn limit is 30 (was 15): recorded `scout` runs
  used at most 5 tool turns, but the broad searches plan mode sends to `Explore` took 14–30.
- `Ω-run` adds `diagnostics_total`, the number of distinct error-like lines in the whole log: when
  it equals the number of `diagnostics`, the list is exhaustive and needs no confirming search.
- `Ω-inspect` returns consecutive lines as one `fragments` entry (its `offset` is the first line's;
  non-adjacent `--search` matches keep separate entries): per-line framing cost about a third more
  tokens than the text itself.
- `shell_guard` allows `'Ω-inspect' ARGS [2>&1] | head|tail|grep|rg|wc ...`, including inert
  quoting such as `--search 'error'`.
- `context_guard` no longer bounds a Read that reaches the end of the file within 300 lines in a
  file of at most 30000 bytes: the bound saved a few lines and cost a second round trip.
- `shell_guard` turns `cd DIR && 'Ω-run' ARGS` into `'Ω-run' --cwd DIR ARGS` instead of wrapping it
  whole, which nested one escaped result JSON inside another. Wrapper calls batched with `;` or
  `&&` are allowed when each call alone is (each keeps its own bounded result); they used to be
  denied, costing a round trip to resend them one by one.
- `install.py` keeps exactly one registration of each Omega hook when its matcher changes.
- `context_guard` records a Read range only after the Read succeeded (the `PostToolUse` matcher is
  now `Read|Edit|Write|MultiEdit`; run `python3 install.py --apply`) and denies only a fully covered
  re-read: a partly covered range is allowed instead of denied.
- Agent instructions require a prior-art check before a new method, algorithm or tool: repo code,
  installed dependency source, upstream reference implementations, then papers. `architect` gains
  `WebFetch`/`WebSearch` and reports the prior art it checked; `robotics-learning` and
  `systematic-debugging` point to the installed framework source and upstream issues.
- `Ω-run` writes back stdout, stderr and the final record first, then flushes all three
  concurrently so they share one ext4 journal commit: small output about 21 → 13 ms per call.
  `result.json` is only replaced once every flush succeeded; the `DATA_LOSS` guarantee is unchanged.
- `shell_guard` accepts a backslash inside a double-quoted wrapper argument when shells and
  `shlex` read it alike (`rg "a\.b\("`); `\$`, `` \` ``, `\!`, `\"` and backslash-newline stay denied.
- `shell_guard` auto-wraps unwrapped commands up to 16 KiB (heredoc scripts included) instead of
  300 bytes, and no longer suggests a nested `Ω-run --shell 'Ω-run …'` for a miswritten wrapper call.
- `Ω-run` reports `NO_MATCH` when a search that is the whole script feeds only plain filters
  (`rg x | cut -c1-80 | head`), not only when it runs alone or last in its pipeline. `pgrep`
  counts as a search, and so does a search stage after the first one
  (`pgrep -af x | grep -v y | cut -c1-80`).
- A `shell_guard` denial of a miswritten wrapper call names its fix: `Ω-inspect` followed by a
  pipe, redirection or second call; operators outside `Ω-run`; `$`, backquote or `!` left live in
  double quotes; a heredoc that does not parse. The check is quote-aware and stays silent when a
  ready-to-run form is offered. The policy is unchanged; the golden fixture is re-recorded for
  the 53 cases whose only change is the added sentence.
- `omega optimize` classifies timed-out runs as `timeout` in `error_classes`, apart from `real`.
- `Ω-run` keeps a pipeline a failure when a search after the first stage exits 1 while stderr is
  discarded (`rg x missing 2>/dev/null | grep -v y`): the first stage's error would be hidden.
- `--shell` runs every command, like a plain shell, keeping pipefail; `--fail-fast` restores
  errexit for scripts whose steps are all required. A fail-fast default stopped exploration
  batches at their first expected failure (a broken build under study, a search without match)
  and cost the model one to three round trips. A failed `--fail-fast` script of several commands
  notes that the later commands did not run.
- A `TIMEOUT` result carries a note against rerunning the command unchanged in the foreground.
- `shell_guard` denies `pkill -f PATTERN` unless the pattern cannot match its own command line
  (`[n]ame`, `^anchor`): it kills the calling shell and the wrapper, leaving no result. The golden
  fixture gains 17 recorded cases covering this rule and every denial fix; one existing case
  (`run inspect … &`) now gets the `Ω-inspect` sentence.
- The benchmark baseline is the `benchmark-baseline` tag instead of a commit hash that a history
  rewrite had removed; an unknown ref ends with a one-line message. Releases require a measured
  benchmark improvement.
- The `context_guard` denial for large evidence files spells out the `Ω-inspect` call and its
  200-line / 30000-byte limits.
- `shell_guard` rewrites a chained command that starts with the `omega` CLI (`omega --help; …`)
  to `Ω-run --shell` like any other command; it was taken for a miswritten wrapper call and denied
  without a ready-to-run command.

### Removed

- The `scripted-robot-scenes` skill, which was specific to one project.
- `Ω-read` / `omega-read` and the `local_read` binary; the installer removes the deployed copies.

### Fixed

- `progress-page`: the task page table of contents no longer lists the journal, which sits
  directly above it, so the link did nothing.

## [1.0.0] - 2026-11-12

First production release of the versioned agent setup.

### Instructions and agents

- One global instruction file (`AGENTS.md`) shared by Claude Code and Codex, with silent-work
  communication rules, robotics safety invariants and a compact Codex base prompt.
- Three workers: `scout` and `architect` (read-only), `coder`; Codex roles generated by `install.py`.
- Skills loaded on demand from their `Use when …` description, including `retro` for
  self-improvement from verified failures.

### Native tools (`omega-tools` crate)

- `Ω-run` evidence executor with bounded JSON, fail-fast shell, `--validation`; `Ω-inspect`,
  `Ω-prune` and the trusted reader `Ω-read`.
- `shell_guard`: wraps policy-clean Bash commands in `Ω-run`, denies file removal (hint: `trash-put`),
  blocks the command if the guard fails (`onFailure: block`).
- `context_guard`: bounds Reads of text over 8 KiB to 200 lines, redirects large evidence to
  `Ω-inspect`, denies re-reads of unchanged lines already in context.
- `context_budget`: one checkpoint reminder before compaction, compaction instructions from
  `PreCompact`, resume from the task file after compaction.
- `omega optimize` (incremental transcript audit, `prefix`, `replay`, `--compare`), `omega resume`,
  `omega --version`.

### Install and operations

- `install.py`: dry run by default, symlinked sources, every hook registered in code,
  `CONFLICT` instead of overwriting foreign files.
- Daily systemd user timers for `omega optimize` and evidence pruning.
- GitHub Actions: tests on every push; GitHub Release created from each `v*` tag.

[Unreleased]: https://github.com/mcaniot/Omega/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/mcaniot/Omega/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mcaniot/Omega/releases/tag/v1.0.0
