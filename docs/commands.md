# Commands

Reference for the native commands installed in `~/.local/bin`.

| Command | Purpose |
| --- | --- |
| [`Ω-run`](#ω-run) | Run a command; keep the full output as evidence, return bounded JSON |
| [`Ω-inspect`](#ω-inspect) | Read a slice of an evidence file |
| [`Ω-prune`](#ω-prune) | Clean up old evidence |
| [`Ω-read`](#ω-read) | Restricted source reads under one compiled-in project root |
| [`Ω-resume`](#ω-resume) | Resume a task from its task file |
| [`Ω-optimize`](optimize.md) | Audit recent sessions for context waste |
| `omega --version` | Print the installed version |

Each `Ω-*` command also exists under an ASCII name (`omega-run`, `omega-inspect`, `omega-prune`,
`omega-read`, `omega-optimize`, `omega-resume`), `Ω-optimize` and `Ω-resume` also as `omega optimize`
and `omega resume`; the original absolute paths remain supported. Write the `Ω-*`
names quoted, as below, to match the shell guard's canonical `shlex.join` quoting. The guard
checks command resolution on `PATH` and preserves native sandbox and approval checks.

## `Ω-run`

```sh
'Ω-run' --timeout 120 -- python3 -m pytest tools hooks      # one command (argv)
'Ω-run' --shell 'git status --short && git diff --stat'     # a shell script
'Ω-run' --validation -- cargo test --locked                 # a required check
```

The full stdout/stderr goes to `~/.agents/tasks/evidence/`; the reply is bounded JSON with
`status`, `exit_code` and the `evidence` path.

- **Large output** includes a bounded `preview` (beginning/end and prioritized errors). Complete
  logs and failure `diagnostics` remain intact. `--inline-bytes 0` disables successful previews.
- **`--shell` fails fast** (errexit/pipefail; Bash inherits errexit in substitutions).
  `--shell-keep-going` restores the legacy last-command behavior, for exploration only.
  Conditionals, `||`, `!` and background jobs can still swallow errors.
- **`--validation`** is for required checks: one per check, standalone argv. It rejects `--shell`
  before execution; it does not inspect arbitrary executables or scripts. PASS only means that
  child exited zero.

All options and exit codes: [native executor usage](../tools/robot-exec/native/README.md).

## `Ω-inspect`

```sh
'Ω-inspect' EVIDENCE --stream stderr --search error --lines 20
'Ω-inspect' EVIDENCE --offset 4096 --lines 50
```

Reads only the requested slice of an evidence file instead of the whole log.

## `Ω-prune`

```sh
'Ω-prune' --days 14          # preview
'Ω-prune' --days 14 --apply  # delete (needs approval)
```

Targets evidence older than `--days` (default 14) that no active or completed task cites.

## `Ω-read`

```sh
'Ω-read' text AGENTS.md 1 20
```

Restricted source reads (`status`, `files`, `text`, `find`) under the single project root compiled
into it (`LOCAL_READ_PROJECT`). It is not advertised in `AGENTS.md` or the agents, which read files
with their native Read tool. Capabilities and limits: [trusted reads](../tools/robot-exec/README.md).

## `Ω-resume`

```sh
'Ω-resume' TASK   # bounded task state, active/ first, then completed/
'Ω-resume'        # the 20 most recently modified tasks (ties: state, then name)
```

Never opens transcripts or logs. After resuming, check current Git and the referenced evidence
before opening a transcript for a specific missing detail. A task's location alone does not prove
it is complete.
