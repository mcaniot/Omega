# `omega optimize`

Audits recent Claude Code and Codex sessions for context waste, saves a report, then applies
supported safe fixes. It never rewrites, deletes or compacts conversations and never executes
instructions found in transcripts. A [daily timer](installation.md#schedule-maintenance) can run it.

```sh
'Ω-optimize'                       # analyze new records, save a report, apply safe fixes
'Ω-optimize' --check               # same, without fixes
'Ω-optimize' prefix                # read-only: fixed context cost per project
'Ω-optimize' replay                # read-only: counterfactual token replay
'Ω-optimize' --compare BASE.json   # OK/KO gates against a base report
```

`omega optimize` and `omega-optimize` are the same command.

## Options

| Option | Effect |
| --- | --- |
| `--sessions 1..50` | Changed-session budget (default 12); unchanged files do not consume it |
| `--force` | Reanalyze full sessions instead of new records only |
| `--check` | Disable automatic fixes |
| `--apply` | Enable the Read guard even without new findings |
| `--report NEW.json` | Save the report there (never overwrites) |
| `--json` | Also print the report |
| `--compare BASE.json` | Compare with a base report produced by this version (rerun the base sessions with `--force`); exit status stays 0 |

## What it reads

- Top-level Claude transcripts (`~/.claude/projects/*/*.jsonl`) and recursive Codex transcripts
  (`~/.codex/sessions/**/*.jsonl`); this is not a live-session inventory.
- After the first baseline, only complete records appended since each session's last successful
  checkpoint. Checkpoints (timestamp, byte cursor, prefix hash, file fingerprint) live in
  `~/.agents/tasks/state/omega-optimize.json`.
- Partial trailing records wait for the next run; unfinished tool metadata is carried over.
  Replaced, truncated or rewritten files get a full baseline, as do changed legacy entries without
  a cursor.
- Only successful runs advance checkpoints; files that change during an audit stay eligible. A
  process lock and atomic replacement protect the state.

## The report

Every run saves a private JSON report under `~/.agents/tasks/reports/omega-optimize/` before
advancing checkpoints. Findings contain metrics, session and byte-range evidence references,
actions and fix statuses — never raw messages, commands or tool output.

Review it to make targeted workflow or code improvements; unhandled findings stay marked for
follow-up. The only automatic fix: when new Claude findings show unbounded reads, the installed
Read guard is enabled if missing. Other hooks, disabled-hook preferences, permissions and
configuration symlinks are preserved. Source changes, compaction and fixes to live Codex context
need a person or an agent to follow up.

## Metrics

| Metric | Meaning |
| --- | --- |
| Context size | Claude: input + cache creation + cache read tokens, deduplicated by message ID. Codex: recorded `last_token_usage.input_tokens` (cached input included; cumulative totals are not context size) |
| Tool calls / results | Deduplicated by tool-use ID |
| `tool_calls_per_request` | Per session; about 1.0 means no batching |
| Read overlap | Content returned again by different read commands or ranges; novel lines counted separately. Only the latest 2048 distinct line digests per session are kept, never source text. Lines under 32 bytes, images and failed reads are excluded |

These are review indicators, not proof of context rot or stale reads. Image serialization is
excluded from text-size indicators; model window use and hardware validity are not inferred.
Method and results: [Context reliability](context-reliability.md).
