---
name: tool-output
description: Capture builds, tests, simulations and profiling as complete disk evidence with bounded diagnostics, preserved exit status and targeted log expansion.
---

Use `'Ω-run' --timeout 120 -- <argv>`.
For environment setup, pipelines or shell builtins, use `'Ω-run' --shell <quoted-script>` and set `--shell-executable /bin/zsh` when needed. Quote argv with shlex.join; never interpolate untrusted text into a shell string.

Plan and group known independent reads in one orchestration call; inspect every result. Related small read-only queries may share one wrapped command if results identify their source and failures remain visible. Keep dependent actions, edits and approvals sequential. Target symbols/ranges before reading; do not concatenate whole files to reduce calls. Bound each query at the source so the combined output fits the inline limit, and allow enough tool-response tokens for the JSON. If output is omitted or truncated, inspect only the missing range; do not rerun the command or reread visible output. For verbose checks, use `--inline-bytes 0` when only status/evidence is needed; inspect diagnostics on failure.

Only bounded JSON reaches the caller; stdout+stderr totaling at most 8192 bytes are inlined as `stdout`/`stderr` (`--inline-bytes N`, 0 disables), provided the JSON fits 12 KiB. Excessive JSON escaping falls back to a summary with diagnostics bounded to 4000 bytes. Do not reread inline output. `evidence/result.json` records command, cwd, duration, child/returned exit codes and stream paths. Exit 124 means timeout; 128+signal means interruption. Logs remain complete. The command is noninteractive and owns its process group: background children are terminated at completion. For long simulations keep the wrapper running with the backend's asynchronous execution/polling; do not detach the child. Set a task-appropriate timeout.

Expand a specific error with `'Ω-inspect' <evidence> --stream stderr --search <literal> --lines 20`. Use returned byte offsets for subsequent slices; never read a log over 8 KiB in full. Full traces remain in stderr.log/stdout.log. Start diffs with --stat and target files; locate symbols before reading sources over 500 lines. Reuse prior reads, expanding only changed or missing context. Do not paste complete logs or repeat the same excerpt in task state. After SIGKILL, RUNNING metadata is inconclusive; verify process liveness before resuming. Never rerun a failing command without a changed hypothesis/input.

Native shell guards require loaded configuration (Codex additionally requires /hooks trust). They preserve native permission checks; they do not cover arbitrary other execution tools. Always call the wrapper explicitly from those tools. Do not claim universal enforcement or token savings from byte counts.
