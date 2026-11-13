# Security policy

## Supported versions

Only the latest release and `main` receive security fixes.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through
[GitHub private vulnerability reporting](https://github.com/mcaniot/Omega/security/advisories/new)
with the affected version, steps to reproduce and the impact.

You can expect an acknowledgement within a week. Once a fix is released, the advisory is published
with credit unless you prefer otherwise.

## Scope

omega runs locally with your user's permissions. Of particular interest:

- ways for a command to bypass `shell_guard` or for a read to bypass `context_guard`;
- `Ω-run` evidence leaking outside `~/.agents/tasks` or losing data while reporting success;
- the installer overwriting files it does not own.
