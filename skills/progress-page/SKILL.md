---
name: progress-page
description: Use when the user wants a progress or tracking page (suivi) for a long task, with steps, statuses, visuals and decisions they can open locally in a browser.
---

# Progress page

One static `index.html`, regenerated from a `state.json` next to it, with images or videos in `img/`. It opens from `file://` with no server and reloads itself every 60 s while visible; the user can pause it. Every render also registers the page in one hub, `~/.agents/progress/index.html`, listing all tracked pages in a single tab (open tasks first, then finished ones): give the user the hub link, not only the page. A task moves to finished when every step is `fait`; it leaves the hub when its `state.json` is deleted or moved. `PROGRESS_HUB` overrides the hub directory (tests).

1. Put the page beside the task outputs (for example `<outputs>/suivi/`), never in the scratchpad. Record its path in the task file.
2. Write `state.json` (page labels and the status keys are French; write the free text in the user's language): `title`, optional `subtitle`, `summary`, `warning`; `steps` (`step`, `status` among `fait`, `en cours`, `à faire`, `bloqué`, optional `note`, `date`); `figures` oldest first (`file` relative to the state directory, `title`, `note`; `.mp4`/`.webm` render as video); optional `log` (`date`, `text`, 5 most recent shown) and `sections` (`title`, `items`). Text is plain; backticks render as code.
3. Render: `python3 ~/.agents/skills/progress-page/scripts/make_page.py <dir>/state.json` (`--refresh N`, N ≥ 5, to change the period). The writes are atomic; a malformed state exits 1 with the faulty key and leaves the old page.
4. Update `state.json` and re-render at each milestone: a step changes status, a new visual, a decision or a blocker. Add a `log` entry with each change. Change `state.json` with targeted edits, not full rewrites, and never read `index.html` back. Keep claims as verified as in the task file; mark unrendered or unconfirmed work as such.
5. Prefer PNG previews at preview resolution (only PNG gets its size reserved); link full renders instead of embedding them.
