# Benchmark

How many tokens omega saves, measured on reproducible synthetic workloads.

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
| Read lines 1–200, then 101–300 | 8,108 | 6,160 | −24 % |
| Read, edit, read again (must not be refused) | 5,027 | 5,027 | 0 % |
| 4000-line build log with one failure, then find it ¹ | 13,689 | 341 | −98 % |
| Read a large saved log to find the failure ¹ | 68,656 | 152 | −100 % |
| **Total** | **143,448** | **18,406** | **−87 %** |

omega's counts vary by a few tokens between runs: they include evidence paths and timestamps.

¹ With plain tools the failure is not even in what the model receives: it lies beyond the Bash
output cap or the Read line limit, so the model would need extra calls to find it. omega returns a
bounded summary plus the one matching line from `Ω-inspect`.

### Previous omega vs current omega

The same workloads replayed on the baseline `4c612c4` (before previews, task-first resume and
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
(`--tokenizer-json`). The savings barely move:

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
| Overlapping Reads | The second Read is narrowed to the missing lines 201–300, which are then allowed |
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
| `--before-ref REF` | Baseline commit (default `4c612c4`) |
| `--after-ref REF` | Candidate commit (default `HEAD`; commit your change first) |
| `--before-run`, `--before-optimize`, `--after-run`, `--after-optimize`, `--context-guard` | Use prebuilt binaries instead of building |
| `--repeats N` | Replays per version (default 7) |
| `--tokenizer-json FILE` | Another tokenizer; `--no-tokens` skips token counts |
| `--output NEW.json` | Report path; existing files are never overwritten |

The report records the refs, binary and script hashes, platform, Python version and tokenizer
hash. History of the earlier measurements: [Context reliability](context-reliability.md).
