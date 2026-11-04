# Native backend audit — 2026-11-03

- Installed: Claude Code 2.1.317; Codex CLI 0.166.0; RTK 0.49.0; Headroom 0.37.0. Verified by each executable's --version.
- Existing native roles: Claude scout/architect/coder/robotics-rl; Codex scout/architect/coder/debugger/reviewer/cleaner/doc. No extra agent framework needed.
- Existing Orca hooks must be retained. Codex also has AgentDeck integration. Claude automatic memory is disabled and autoCompactWindow is 150000.
- Existing efficient-execution skill has reusable retry breakers and milestone handoffs. Old Claude coder/robotics-rl tail pipelines discarded evidence; coder prohibited all integration/simulation checks.
- Obsolete Omega pre-edit hook referenced missing ~/.local/share/omega/bin/omega.py. Two apply_patch attempts failed with this error, including after parent removed the backed-up definition: active runtime retained stale hooks. Parent authorized normal wrapper-captured writes to the assigned global files. No dummy hook or trust bypass was created.
- RTK and Headroom are installed; neither appears in current Claude/Codex hook definitions. Headroom's proxy would expand scope; RTK generic full-evidence metadata guarantees were not established.

## Supported interface and limits

Official references inspected:
- https://learn.chatgpt.com/docs/hooks
- https://code.claude.com/docs/en/hooks

Bash is the canonical shell matcher, including Codex exec_command and nested code mode. Hook shell input is tool_input.command. Native PreToolUse denial is hookSpecificOutput with hookEventName=PreToolUse, permissionDecision=deny and permissionDecisionReason. Returning {} leaves normal execution permissions intact.

Codex rewrite requires allow plus updatedInput; Claude allow can bypass normal approval. The implemented guard does not rewrite or return allow. It requires a canonical shlex.join command invoking the exact ~/.agents/tools/robot-exec/native/run path with run, inspect or prune, or the fixed local-read forms. Outer shell operators/substitution are rejected; quoted --shell contents are captured inside the wrapper.

New Codex hooks require native /hooks trust; no CLI trust subcommand was found. Do not forge trusted_hash or use global trust bypass. Hooks can fail open on operational errors and specialized execution paths can bypass them. This is a guard for supported shell calls, not a universal security boundary. Native runtime activation is separate from unit-test success.

## Verification

The guard is the Rust binary `shell_guard` (`rust/`); `tools/test_shell_guard.py` drives it with hand-written policy cases and a recorded fixture (`OMEGA_SHELL_GUARD_BINARY` overrides the deployed binary). Coverage: run/inspect/prune, quoted shell bodies, outer shell injection, path/interpreter spoofing, invalid payloads, non-shell tools, malformed stdin JSON, fail-closed internal errors.

Native hook activation, fresh-session discovery, live isolation, compaction and token benchmarks remain the integrating agent responsibility.
