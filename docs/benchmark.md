# Benchmark

What omega saves, at two levels:

- **Tool output** (this page down to [Options](#options)): tokens the tools return on
  reproducible synthetic workloads. Deterministic, no model call; it guards the binaries against
  regressions.
- [**End to end**](#end-to-end): cost, turns, wall time and correctness of real model sessions,
  in headless A/B runs (Claude Code and Mistral Vibe) and in recorded sessions. This is what the
  bill reflects.

The tool-output saving is large, but tool output is a small share of a session's cost, which is
dominated by cache reads and writes of the whole context and by subagents.

```sh
uv run --no-project --with tokenizers python3 tools/benchmark_context.py
```

One command: it builds the binaries in throwaway trees, runs every scenario, prints both tables
below and writes the full JSON report to `~/.agents/tasks/reports/omega-benchmark/`. No model call,
no network access except the one-time tokenizer download, no change to the checkout.

## Results

Run of 2026-11-12 on `dd4c8b8`. Tokens are counted with the GPT-2 tokenizer (pinned by sha256),
not with Claude's tokenizer or billing, so compare ratios rather than absolute numbers.

### Plain Claude Code tools vs omega

What the model receives for the same task, with and without omega's guards and `Ω-run`.
Deterministic: one run is enough.

| Scenario | Plain Claude Code | omega | Change |
| --- | ---: | ---: | ---: |
| Read a 3000-line file without a limit | 42,952 | 4,174 | −90 % |
| Read the same small file twice | 5,016 | 2,552 | −49 % |
| Read lines 1–200, then 101–300 | 8,108 | 8,108 | 0 % |
| Read, edit, read again (must not be refused) | 5,027 | 5,027 | 0 % |
| 4000-line build log with one failure, then find it ¹ | 13,689 | 341 | −98 % |
| Read a large saved log to find the failure ¹ | 68,656 | 152 | −100 % |
| **Total** | **143,448** | **20,354** | **−86 %** |

omega's counts vary by a few tokens between runs: they include evidence paths and timestamps.

¹ With plain tools the failure is not even in what the model receives: it lies beyond the Bash
output cap or the Read line limit, so the model would need extra calls to find it. omega returns a
bounded summary plus the one matching line from `Ω-inspect`.

### Previous omega vs current omega

The same workloads replayed on the baseline tag `benchmark-baseline` (`9129c9a`; before previews, task-first resume and
fail-fast shell) and on the current version. Medians of 15 replays.

| Workload | Returned tokens | Reads | Local latency (ms) |
| --- | ---: | ---: | ---: |
| Oversized output: recover beginning, middle error, end | 358 → 141 | 4 → 1 | 6.5 → 15.0 |
| Small inline output | 92 → 67 | 1 → 1 | 2.5 → 11.7 |
| Resume a task: transcript-first → task-first | 297 → 105 | 3 → 1 | 6.7 → 1.1 |
| Mandatory shell failure (now reported as FAIL) | 93 → 66 | 0 → 0 | 3.1 → 12.3 |
| Advisory read audit (`Ω-optimize --check`) | 408 → 699 | 0 → 0 | 19.1 → 19.7 |
| **Total** | **1,248 → 1,078 (−14 %)** | | |

What got worse, and is tracked:

- The read-audit report is longer (+72 %): it now carries more findings per session.
- `Ω-run` costs about 12–15 ms per call instead of 2–7 ms. Since `be61af2` it `fsync`s stdout,
  stderr and `result.json` before reporting, so that a delayed write error (disk full, I/O error)
  is reported as `DATA_LOSS` instead of a false PASS. This is deliberate data-loss protection,
  not a bug. Each `fsync` forced its own ext4 journal commit (about 9 ms, two in series, so
  21–34 ms per call). The three files are now written back first and flushed concurrently, so
  they share one commit (strace); `result.json` replaces the previous record only once every
  flush succeeded. The remaining ~9 ms is that single commit. Latency depends on disk load and
  varies by several milliseconds between runs.

### Tokenizer check

Claude's current tokenizer is not public, so the same run was repeated with two other tokenizers
(`--tokenizer-json`) in a separate run, so its GPT-2 total differs from the table above. The
savings barely move:

| Tokenizer | Plain → omega | Change | Previous → current omega |
| --- | ---: | ---: | ---: |
| GPT-2 (default, pinned) | 143,448 → 18,406 | −87 % | −14 % |
| Legacy Claude (`Xenova/claude-tokenizer`) | 135,885 → 15,925 | −88 % | −13 % |
| GPT-4o o200k (`Xenova/gpt-4o`) | 135,964 → 17,268 | −87 % | −15 % |

**Against the real Claude tokenizer**, measured through Claude itself: four identical subagents each
Read one file (≤ 200 lines) and replied `ok`. The context size of the request after the Read,
minus that of a one-line control file, is the Claude token count of the Read result.

| Sample (as the Read tool returns it) | GPT-2 | Claude | Claude / GPT-2 |
| --- | ---: | ---: | ---: |
| 200 lines of code (`module.py`) | 4,108 | ≈ 4,089 | 1.00 |
| 200 lines of build log | 6,703 | ≈ 7,564 | 1.13 |
| omega's answer for the build-log scenario (`Ω-run` + `Ω-inspect`) | 313 | ≈ 306 | 0.98 |

Claude counts code and omega's JSON like GPT-2, and build logs about 13 % higher. Since the
plain-tool side is mostly logs, the GPT-2 figures slightly *understate* the saving: rescaled with
these ratios the total is about 153,800 → 18,200 tokens (−88 %). One sample per type; the control
removes the fixed system and tool overhead (13,071 tokens).

## What each scenario checks

Each scenario also asserts correctness, so a result that saves tokens by losing information fails
the run instead of counting as a win:

| Scenario | Asserted |
| --- | --- |
| Large Read | The Read is bounded to lines 1–200 of 3000 and says so |
| Same file twice | The second Read is refused as `already in context` |
| Overlapping Reads | The second Read is allowed and recorded: only a fully covered Read is refused |
| Read after edit | The Read after an edit is **not** refused and contains the new line |
| Build log | The complete log is kept on disk; `Ω-inspect` finds the failure |
| Saved log | A Read of an evidence file is redirected to `Ω-inspect` |
| Oversized output | Beginning, middle error and end are all recovered; the full log hash matches |
| Resume | `Ω-resume` returns the task file unchanged and untruncated |
| Shell failure | A failing step in `--shell` is reported, not masked |
| Read audit | The audit never modifies the transcript it reads |

## Method and limits

- **Plain tools are modelled** from Claude Code's defaults: Read returns up to 2000 numbered lines;
  Bash output is cut at 30,000 characters (`BASH_MAX_OUTPUT_LENGTH`), modelled as its first
  30,000 characters. omega results come from the real binaries (`context_guard`, `Ω-run`,
  `Ω-inspect`) fed the same hook payloads Claude Code sends.
- **The workloads are synthetic** and chosen to show where omega acts. Real sessions mix them
  with work omega does not change; for real transcripts use `'Ω-optimize' replay`
  (see [`omega optimize`](optimize.md)).
- **Agent behavior is not measured.** The scenarios replay a fixed sequence of calls; they do not
  show that a model always takes that path.
- **Latency** is local process time, including startup, not remote tool transport.

## Options

| Option | Effect |
| --- | --- |
| `--before-ref REF` | Baseline commit (default: the `benchmark-baseline` tag; `git fetch --tags origin` if it is missing) |
| `--after-ref REF` | Candidate commit (default `HEAD`; commit your change first) |
| `--before-run`, `--before-optimize`, `--after-run`, `--after-optimize`, `--context-guard` | Use prebuilt binaries instead of building |
| `--repeats N` | Replays per version (default 7) |
| `--tokenizer-json FILE` | Another tokenizer; `--no-tokens` skips token counts |
| `--output NEW.json` | Report path; existing files are never overwritten |

The report records the refs, binary and script hashes, platform, Python version and tokenizer
hash. History of the earlier measurements: [Context reliability](context-reliability.md).

## End to end

### Claude Code and Mistral Vibe

Measured on 2026-11-23: Claude Code with Opus 5.5 (`claude -p`) and Mistral Vibe 3.1.3 with Mistral
Medium 3.5 (`vibe -p --auto-approve`), headless, with and without omega. One session per arm and
repetition solves the same 9 tasks as below, with hidden checks: the 5 generic ones and 4 robotics
ones (quaternion order against a contract, a joint-limit trap whose limit table must not change,
dt/decimation with the simulator constants fixed, observation order against a contract). The
harness of 2026-11-16 was lost, so the robotics tasks were rewritten: compare with the tables
below only loosely. Arms run 4 at a time.

- Plain Claude Code: `--safe-mode`. Plain Vibe: `HOME` and `VIBE_HOME` point to empty
  directories holding only Vibe's `config.toml`, so no AGENTS.md, skills, subagents or hook.
  Setting `VIBE_HOME` alone is not enough: Vibe still lists the skills in `~/.agents/skills`.
- omega arms: the installed setup (`~/.claude`, `~/.vibe`).

| Arm | Correct | Cost (USD) | Turns | Input tokens (uncached) | Output | Wall (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Plain Claude Code | 27/27 | 0.202 | 11.3 | — | 2,839 | 37 |
| Claude Code + omega | 27/27 | 0.228 | 8.3 | — | 2,585 | 31 |
| Change | = | +13 % | −26 % | | −9 % | −16 % |
| Plain Vibe | 45/45 | — | 43.8 | 1,757,539 (85,116) | 5,910 | 213 |
| Vibe + omega | 45/45 | — | 46.6 | 1,699,252 (65,358) | 7,158 | 238 |
| Change | = | | +6 % | −3 % (−23 %) | +21 % | +12 % |

- **Claude Code:** omega takes fewer turns and less time, but costs 13 % more (per repetition
  +7 %, +23 %, +11 %): its configuration prefix adds about 3.9k cache-written tokens per session
  (17,350 vs 13,429), and the plain arm needed only 9–13 turns. Cache reads are equal (≈ 187k).
- **Vibe:** Vibe reports `cost_usd` 0 on a subscription, so tokens are compared. Mistral Medium
  makes one tool call per turn (41–60 calls per session in both arms), and omega does not change
  that. omega's hook does run (commands go through `Ω-run`), but it doubles the system prompt
  (11.3k → 23.0k tokens per request). Uncached input is 23 % lower, output 21 % higher, turns and
  wall time slightly higher (one omega session took 61 turns and 452 s).
- Vibe means are over 5 paired repetitions (0, 2, 10, 11, 12). Repetitions 0–2 ran with a 60-turn cap,
  and in repetition 1 Vibe + omega hit it before writing its answers (5/9, the 5 code fixes were
  correct); the following repetitions used a 100-turn cap. Three later sessions stopped on
  Vibe's plan quota (`model_billing_denied`) and are excluded, as is repetition 1.
- Three Claude repetitions and five Vibe ones: differences under about 10–15 % are within
  run-to-run spread. The tasks are short and rarely produce large outputs, where omega's tools act.

### Claude Code, earlier runs

Measured on 2026-11-16 with Opus 5.5 (`claude -p`, headless). Costs are the API-equivalent
`total_cost_usd` that Claude Code reports, not a subscription bill.

#### Headless A/B

One session per arm and repetition solves 9 independent tasks with hidden checks (5 generic: build
log, NaN log, diff review, code navigation, edit loop; 4 robotics: quaternion frame, joint-limit
trap, dt/decimation, observation order). Arms run in parallel, 3 repetitions each. Means per
session.

**Current version** (after the changes below; Claude Code's advisor tool disabled, so only Opus
runs):

| Arm | Correct | Cost (USD) | Turns | Cache read | Cache write | Output | Wall (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Plain Claude Code, no instructions | 27/27 | 0.306 | 16.3 | 351,419 | 19,360 | 4,052 | 52 |
| omega, real setup | 27/27 | 0.233 | 8.3 | 173,539 | 17,846 | 2,776 | 35 |
| Change | = | −24 % | −49 % | −51 % | −8 % | −31 % | −33 % |

omega is cheaper in every repetition (−34 %, −8 %, −27 %): half the turns means half the context
re-read per session. In the run before, with the advisor enabled (`advisorModel: fable`), plain
Claude Code consulted it in 2 of 3 repetitions (about 30k uncached input tokens to Fable each,
0.70 and 0.76 USD sessions) and omega never; that run gave 0.632 vs 0.266 USD, with the
advisor's cost dominating the gap.

**Earlier version** (morning of 2026-11-16, before removing the `Ω-run` rule):

| Arm | Correct | Cost (USD) | Turns | Cache read | Cache write | Wall (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Plain Claude Code, no instructions | 27/27 | 0.460 | 18.7 | 413,548 | 20,343 | 74 |
| Plain tools + tool-neutral instructions | 27/27 | 0.237 | 13.3 | 265,342 | 14,307 | 52 |
| omega + the same instructions | 27/27 | 0.242 | 8.7 | 194,974 | 17,318 | 40 |
| omega, real setup | 27/27 | 0.290 | 10.0 | 218,197 | 22,336 | 39 |

- With the same instructions, omega costs the same (+2 %; per repetition −3 %, +4 %, +7 %) with
  35 % fewer turns, 27 % fewer cache reads and 23 % less wall time: its larger fixed prefix (hook
  and tool context) offsets the shorter sessions.
- omega's real setup against plain Claude Code: −24 % on average, but one plain repetition
  consulted the Fable advisor (0.760 USD); without that repetition the gap is about −3 %.
- Most of the gain over plain Claude Code comes from the instructions, not from the tooling.

A follow-up A/B (same tasks, same omega tooling, 3 repetitions) removed the AGENTS.md rule that
every command must go through `Ω-run`, since `shell_guard --allow-clean` already wraps clean
commands: 27/27 in both arms, 0.293 → 0.265 USD (−10 %), 11.3 → 9.7 turns, 56 → 38 s. Costs
overlap between repetitions. The rule was removed for Claude and kept for Codex, whose guard
denies unwrapped commands.

### Recorded sessions

Replay of the user's recorded Claude Code sessions, main conversation and subagents:

- Per request, the main conversation costs 25 % less with omega; per session, cost is 23 %
  higher, and subagents account for all of the difference. Delegated work costs about half as much
  per unit of work, but delegation adds work.
- Requests after more than 1 h idle rewrite the whole context at the cache-write price: 35 % of all
  cache writes. Restarting at those points from the task file would have cut cost by 13–15 % on
  69 Opus sessions; a notice that blocked the first idle prompt to offer it was removed, since a
  blocked prompt stops agents driven by another session.

### Limits

Checked for bias on the current A/B:

- **No cache advantage.** Both arms read the same ~9.8k-token shared prefix from cache on their
  first request; omega writes its own extra ~6.3k prefix again in every repetition, so it is not
  helped by earlier runs. Arms run at the same time, with the same tools, turn limit and checks;
  plain runs with `--safe-mode` (no customization).
- **Tools vs instructions.** The current table compares the full setup with uninstructed Claude
  Code. With the same tool-neutral instructions in both arms, the current version is not cheaper:
  0.244 vs 0.226 USD (+8 %; per repetition +21 %, +13 %, −12 %), 7.3 vs 7.7 turns, 34 vs 33 s,
  27/27 both (earlier version: +2 %). The saving comes from omega's instructions (fewer, batched
  calls); its tools add a fixed prefix and pay off only on large outputs, which these tasks rarely
  produce once the model batches its calls.
- **Large outputs.** A second A/B with the same instructions on three commands printing 0.25–1.2 MB
  (training run with a NaN, build with one error among decoy warnings, test run with two unnamed
  failures; 3 repetitions): omega 0.105 vs 0.112 USD, 3.7 vs 5 turns, 9/9 vs 8/9 correct. In both
  arms Opus 5.5 redirected the output to a file and searched it itself, so no tool result exceeded
  3 KB and the tools had nothing to bound. Their gain shows only when a model does dump a large
  output, as in recorded sessions (about 1.8M tokens saved over 100 sessions).
- **Statistics.** Per-repetition cost changes are −34 %, −8 %, −27 %: a 95 % interval of about
  −56 % to +9 %. Turns (−25 % to −61 %) and wall time are consistently lower.
- **Task choice.** The tasks were written alongside omega: two exercise large logs and four
  exercise the robotics rules of its AGENTS.md. Both arms solve all of them, so the benchmark
  measures cost, not quality.
- **No delegation.** These sessions never spawn subagents; in recorded sessions subagents made
  omega 23 % more expensive per session, so real use can differ from this result.
- Three repetitions per arm: differences under about 10 % are within run-to-run spread.
- Synthetic tasks and a single model; agent behavior varies between runs.
- The A/B harness is not shipped in this repository yet, and the recorded-session figures reflect
  one user's work mix.
