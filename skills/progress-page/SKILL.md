---
name: progress-page
description: Use when starting or updating a mission expected to run more than about an hour unattended (runs, renders, simulations, trainings) or when the user asks for a progress or tracking page (suivi) with steps, statuses, visuals and decisions to follow in a browser.
---

# Progress page

One static `index.html` per mission, regenerated from a `state.json` next to it, with images or videos in `img/`, opened from `file://`. Every render registers the page in one hub, `~/.agents/progress/index.html`, that the user keeps open: tasks with a question first, then blocked, active, finished. Update at least every 2 h during a long run, or the hub flags the task "sans nouvelles". `PROGRESS_HUB` overrides the hub directory (tests).

S=~/.agents/skills/progress-page/scripts/make_page.py

1. Create the page when the mission starts, without waiting to be asked, beside the task outputs (for example `<outputs>/suivi/`), never in the scratchpad. Link both ways: first render with `--task <task> --agent <your session name>`, and add `Progress page: <dir>/state.json` to the task file.
2. Write `state.json` once (page labels and status keys are French; free text in the user's language): `title`, optional `subtitle`, `summary`, `warning`; `steps` (`step`, `status` among `fait`, `en cours`, `à faire`, `bloqué`, optional `note`); optional `sections` (`title`, `items`). Text is plain; backticks render as code. Render with `python3 $S <dir>/state.json`.
3. At each milestone, update and render in one command without reading the state, single-quoting free text (no shell expansion): `python3 $S <dir>/state.json --step '2=en cours::note' --log 'texte' --fig 'img/x.png=Titre::note'`. Steps are numbered from 1 as on the page; `N=STATUS` keeps the note, `N=STATUS::` clears it. `--add-step TEXT`, `--warn TEXT`; `--fig` paths are relative to the state directory and must exist. When a user decision is needed, `--ask 'question'`; clear with `--ask ''` once answered. Never read `index.html`.
4. Continuity: one page per mission, across sessions. Before a handoff or `/clear`, `--log 'handoff : <next action>'`. On resume (after `/clear`, compaction or a handoff), take the page from the task file's `Progress page:` line (else `python3 $S --find <task>`), never create a second one; run `--show` for the numbered steps and `--agent <your session name>`, which logs the takeover.
5. Keep claims as verified as in the task file; mark unrendered or unconfirmed work as such. Prefer PNG previews at preview resolution; link a full render with a figure's `full` key.
6. `python3 $S --all` re-renders every registered page; `python3 $S <state> --forget` removes a finished or deleted mission from the hub. A malformed state or flag exits non-zero with the faulty key and leaves the old page.
