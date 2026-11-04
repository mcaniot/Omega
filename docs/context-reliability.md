# Context reliability: changes and measurements

## Contracts

- Oversized execution output adds `preview` fragments: beginning/end of each
  stream and prioritized errors, with stream/line/byte locations. Existing failure
  `diagnostics`, full evidence, status codes, privacy and process cleanup remain.
  Preview plus diagnostics fit the existing 4000-byte JSON budget. Small inline
  output is unchanged; `--inline-bytes 0` keeps successful execution silent.
- `--shell` defaults to errexit/pipefail; bash also inherits errexit in command
  substitutions. `--shell-keep-going` explicitly restores legacy semantics,
  including POSIX sh, and requires `--shell`. Conditionals, `||`, `!`, background
  jobs and explicit shell-option changes can consume failures: required checks
  must remain standalone argv or unconditional top-level commands. PASS certifies
  child exit zero, not arbitrary branches, output text or hardware validity.
  `--validation` enforces argv-only invocation by rejecting `--shell` before
  execution. Run each check independently; arbitrary child programs remain trusted
  to propagate their own failures, so this is not a general shell sandbox.
- `omega resume [TASK]` lists/reads bounded task state before historical material,
  preferring active/ over completed/. It never follows evidence references or
  opens transcripts, rejects traversal/symlink task files and nonregular files,
  and marks truncation. Task location alone does not prove completion; verify
  current Git and relevant evidence before deciding. No commands from task text
  are evaluated.
  Candidate lists are sorted by modification time descending across both task
  directories before the 20-item limit; equal times sort by state and basename.
- Read auditing compares returned lines, not command identity. It unwraps recorded
  Read/exec/inspect output, excludes images/errors/lines shorter than 32 bytes,
  preserves significant whitespace, and retains at most 2048 distinct SHA-256
  line digests per session across incremental checkpoints. Reports distinguish
  overlapping and novel lines. Overlap is advisory, can occur across different
  sources, and is never used to suppress a read or certify file freshness.

## Reproducible local replay

[Raw samples and binary/tokenizer fingerprints](context-reliability-benchmark.json).
Baseline source: `4c612c4a794058619df6d2dec38a75573a73091e`, independently built
offline with the same release toolchain as the modified source. Deterministic
synthetic data, seven replays per version, local CPU, medians below. No model calls,
network access or installation are performed by the benchmark.

| Workload | Returned tokens before → after | Reads before → after | Local latency ms before → after |
| --- | ---: | ---: | ---: |
| Oversized read; recover beginning, middle error, end | 347 → 152 | 4 → 1 | 6.70 → 5.66 |
| Small inline output | 91 → 91 | 1 → 1 | 2.45 → 2.68 |
| Transcript-first vs task-first policy replay | 292 → 104 | 3 → 1 | 6.97 → 1.16 |
| Mandatory shell failure | 91 → 90 | 0 → 0 | 3.14 → 3.59 |
| Advisory read audit | 403 → 562 | 3 → 3 source reads | 19.40 → 19.71 |

The shell fixture previously masked the failure; it now returns FAIL. The audit
detects two overlapping reads and two novel lines; it does **not** automatically
eliminate source reads. Error/status fidelity and complete-log hashes are asserted
by the replay. Small-output latency varied by 0.23 ms; timings include process
startup, not remote tool transport, and do not establish statistical significance.

Tokens are measured using the existing `tokenizers` package and a local GPT-2
tokenizer JSON (fingerprint in the report), not bytes/4 estimates or provider billing
telemetry. Returned JSON and command tokens are recorded separately. AGENTS.md
increases from 975 to 1085 tokenizer tokens (+110 always-loaded tokens); detailed
procedures remain in the on-demand skill. The resume comparison is an explicit
workflow replay, not evidence that a model will always follow that workflow.

Reproduce with existing Python/tokenizer tooling; no runtime dependency is added:

```sh
'Ω-run' --timeout 120 -- PYTHON tools/benchmark_context.py \
  --before-run BEFORE_RUN --after-run rust/target/release/run \
  --before-optimize BEFORE_OPTIMIZE --after-optimize rust/target/release/optimize \
  --tokenizer-json LOCAL_TOKENIZER_JSON --repeats 7 --output NEW_REPORT.json
```

The tokenizer option is optional; absent it, token counts are explicitly null.
Reports use exclusive creation and never overwrite existing evidence. The report
records the script hash, binaries, platform, Python version and tokenizer hash.

## Validation

- 99 tools tests: execution, parser, overlap/novel data, checkpoints, task-first,
  traversal/symlinks, shell safety policy, aliases, settings and context guards.
- 3 golden tests (legacy shell behavior explicitly selected; additive previews and
  new help text handled separately), plus 5 native boundary tests.
- Release build offline; installed run/optimize hashes and 0700 regular-file modes
  checked against the build; installed `omega resume` smoke passed.
- No dependencies, hardware operations, transcript rewrites or read suppression added.
