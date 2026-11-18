---
name: code-review
description: Use when reviewing a merge request, pull request, branch or local diff, drafting review comments, or publishing approved review comments to GitLab or GitHub.
---

# Code review

Senior review focused on functional correctness, simplicity and minimal change. A review with no finding is valid; never fill a quota. Talk to the user in their language; write review comments in English unless asked otherwise.

## Analyze

1. Read the repository instructions, the MR/PR description, the current diff and the existing discussions (`glab mr view/diff`, `glab api projects/:fullpath/merge_requests/IID/discussions`; `gh pr view/diff`, `gh api repos/OWNER/REPO/pulls/N/comments`). Record the head SHA.
2. Read the code around each hunk and the affected callers and callees to confirm the actual behavior; never judge from the diff alone.
3. Look for bugs, regressions, edge cases, broken contracts, concurrency, resource and performance problems. Robotics changes: check the AGENTS.md Safety contracts (units, frames, ordering, shapes, timing, limits, realtime paths).
4. Check tests and CI (`glab ci status`, `gh pr checks`). Label each check `run`, `inspected` or `not verified`. Only bounded local checks; nothing that reaches a robot.
5. Report only problems the change introduces or worsens; no general repository audit.
6. Back every finding with the exact `file:line`, the triggering scenario and the concrete consequence. Anything unconfirmed is stated as a hypothesis.
7. SonarQube only when asked: read the available results; never present a manual analysis as a SonarQube run.
8. Large or risky diff: optionally get an independent pass from `Plan`, given the diff and paths, not your findings; verify its claims before keeping them.

## What to look for

Apply with judgment, not mechanically; state the concrete gain before proposing a change. Prefer a local fix; ask for a redesign only when the problem justifies it. No taste-only remarks.

- Readable, direct, modular code with few abstractions.
- Duplication and competing sources of truth.
- Contracts shared between components or languages: consistent keys, types, units and values.
- Named constants for configurable thresholds and repeated strings that carry a contract; enums for closed states or values when they give a real guarantee.
- Fallbacks, reconnections and defensive conditions whose need is not demonstrated.

## Comment style

- Usually one or two sentences: natural, direct, precise, plain words.
- No introduction, praise, boilerplate or textbook explanation. No "we" speaking for the author, no reflex "Can we…" questions.
- Confirmed bug: state the problem and its consequence. Debatable choice: ask one targeted question.
- Model the tone on the user's real review comments, preferably ones they edited or approved (fetch them from the platform when available); never invent examples of their style.
- Include every remark the user gave and flag those the code does not confirm.
- Format each comment as a [Conventional Comment](https://conventionalcomments.org/): `<label> [(decorations)]: <subject>`, then an optional short discussion. Labels: `issue`, `suggestion`, `question`, `nitpick`, `todo`, `thought`, `chore`, `note`; no `praise`. Mark every `issue` and `todo` `(blocking)` or `(non-blocking)`; a confirmed bug is `issue (blocking)`, an optional improvement `suggestion (non-blocking)`.

## Proposal

Default output, and the end of the task unless publication is approved: `Blocking` (confirmed bugs) then `Optional`; per comment the `file:line`, a short justification in the user's language and the exact English text; then the checks with their `run/inspected/not verified` label. A review request alone never authorizes publishing.

## Publish (only after explicit approval)

1. Re-fetch the head SHA and diff. If they changed, re-check each approved comment; present any invalidated or moved comment in its new form and wait.
2. Skip comments that duplicate an existing discussion.
3. Post only the approved comments, inline on their line when possible:
   - GitLab: `glab api projects/:fullpath/merge_requests/IID/discussions --input BODY.json`, body `{"body": …, "position": {"position_type": "text", "base_sha", "start_sha", "head_sha" (from the MR `diff_refs`), "old_path", "new_path", "new_line" or "old_line" for a removed line}}`. Bracketed `-f 'position[...]'` names are rejected in a JSON body.
   - GitHub: `gh api repos/OWNER/REPO/pulls/N/comments` with `body`, `commit_id`, `path`, `line`, `side`.
   - Fall back to a general note citing `file:line` only when the inline post fails.
4. Never edit code, resolve discussions, approve or merge without an explicit request.
5. Return the link of each posted comment.
