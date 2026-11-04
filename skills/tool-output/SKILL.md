---
name: tool-output
description: Capture builds, tests, simulations and profiling as complete disk evidence with bounded diagnostics, preserved exit status and targeted log expansion.
---

Use `/home/mcaniot/.agents/tools/robot-exec/native/run run --timeout 120 -- <argv>`.
For environment setup, pipelines or shell builtins, use `run --shell <quoted-script>` and set `--shell-executable /bin/zsh` when needed. Quote argv with shlex.join; never interpolate untrusted text into a shell string.

Only compact JSON reaches the caller; when stdout+stderr total at most 2048 bytes they are inlined as `stdout`/`stderr` (`--inline-bytes N`, 0 disables), so no follow-up read is needed. `evidence/result.json` records command, cwd, duration, child/returned exit codes and stream paths. Exit 124 means timeout; 128+signal means interruption. Logs remain complete. The command is noninteractive and owns its process group: background children are terminated at completion. For long simulations keep the wrapper running with the backend's asynchronous execution/polling; do not detach the child. Set a task-appropriate timeout.

Expand a specific error with `run inspect <evidence> --stream stderr --search <literal> --lines 20`. Use returned byte offsets for subsequent slices. Full traces remain in stderr.log/stdout.log. Do not paste complete logs or repeat the same excerpt in task state. After SIGKILL, RUNNING metadata is inconclusive; verify process liveness before resuming. Never rerun a failing command without a changed hypothesis/input.

Native shell guards require loaded configuration (Codex additionally requires /hooks trust). They preserve native permission checks; they do not cover arbitrary other execution tools. Always call the wrapper explicitly from those tools. Do not claim universal enforcement or token savings from byte counts.
