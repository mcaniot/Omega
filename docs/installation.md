# Installation

How to install omega, keep it up to date and schedule its maintenance.
For what gets installed where, see [Architecture](architecture.md#layout).

## Install or update

```sh
python3 install.py            # dry run: lists what would change
python3 install.py --apply    # create links, regenerate the Codex agents, build and install the Rust binaries
```

`Ω-read` reads only one project, chosen at build time: run the installer as
`LOCAL_READ_PROJECT=/absolute/path/to/project python3 install.py --apply`. Without it the
installer reports `UNSET` and `Ω-read` refuses every request; everything else works.

Then:

1. Keep `~/.local/bin` on `PATH`.
2. Restart Claude Code and start a new Codex conversation. Existing conversations can keep their
   original prompts, and a newly installed hook does not reclaim context already used.
3. Check the result: `omega --version`, and the last installer line should read `0 need attention`.

Keep this checkout on a branch that contains these files. To update, pull, run the
[tests](development.md#tests), then run `install.py --apply` again.

| Option | Effect |
| --- | --- |
| *(none)* | Dry run |
| `--apply` | Write changes |
| `--budget` | Print the bytes loaded into every session |
| `--responses-only --apply` | Update only response prompts and their selection |
| `--context-only --apply` | Install the context guard, the `omega` CLI and command links only |

## What takes effect when

- **Instructions, agents, skills** are symlinked: edits take effect at the next session.
- **Rust binaries** change only with `install.py --apply`, so test first.
- **Hooks** are registered by the installer; see [Hooks](hooks.md#registration).

## What the installer never overwrites

- An existing file or command link is replaced only if it already links into this repository or
  has identical content. Otherwise it is reported as `CONFLICT` and left untouched.
- Response settings are merged into the existing Codex TOML and Claude JSON without resetting
  models, permissions or hooks. A different existing Codex `model_instructions_file` is reported
  as a conflict. Model caches may regenerate and do not control these overrides. Tool displays and
  required approval prompts remain visible.
- An existing Codex role file is rewritten only if it has the exact shape the installer writes.
- Only retired Omega roles listed in `RETIRED_AGENTS`, their legacy `[agents.*]` registrations and
  dangling links are removed. Other agent files, TOML and configuration are never deleted.

## Schedule maintenance

Two user systemd timers run [`omega optimize`](optimize.md) and the evidence cleanup
(`omega-prune --apply`, evidence older than 14 days that no task cites) once a day:

```sh
systemctl --user link "$PWD"/systemd/omega-{optimize,prune}.{service,timer}
systemctl --user enable --now omega-optimize.timer omega-prune.timer
systemctl --user list-timers 'omega-*'   # check
```
