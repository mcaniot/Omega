---
name: retro
description: Use when the user asks for a retrospective, lessons learned or self-improvement of the agent setup, or when closing a task that needed a user correction, a failed approach, a repeated tool error or a context compaction.
---

# Retro: verified failures become small durable rules

Goal: the same mistake never costs tokens twice, and the always-loaded context does not grow.

## 1. Collect evidence, not impressions

- `'Ω-optimize' --check`, then the saved report: workflow errors, avoidable re-reads, compactions, repeated calls.
- The task file (decisions, failed approaches) and the user's corrections in this session.
- Open a transcript or evidence slice only to confirm one specific candidate.

## 2. Keep a candidate only if all hold

- Observed in evidence, with a reference (task file, evidence path, report finding).
- It had a real cost: a user correction, two or more occurrences, or a safety risk.
- It states a trigger and an action: `When <situation>, <do this>`.
- No existing rule already covers it. If one does and was ignored, sharpen that rule instead.
- It does not assert units, frames, signs, joint or tensor order, limits, timing or hardware semantics. Those need a confirmed contract, never a lesson.

## 3. Route to the narrowest place that will be read

| Lesson about | Destination |
| --- | --- |
| Ω-run/Ω-inspect usage, statuses, evidence | `skills/tool-output/references/lessons.md` |
| Realtime C++, locks, allocation, lifetime | `skills/cpp-quality/references/lessons.md` |
| Isaac Lab, RL, imitation, policies, datasets | `skills/robotics-learning/references/lessons.md` |
| Latency, jitter, throughput, profiling | `skills/robotics-performance/references/lessons.md` |
| Choice or sufficiency of checks | `skills/robotics-validation/references/lessons.md` |
| Root-cause method, flaky or solver failures | `skills/systematic-debugging/references/lessons.md` |
| Scripted scenes, poses, camera, export | `~/.agents/private-skills/scripted-robot-scenes/references/lessons.md` |
| One repository (paths, build, launch, contracts) | that repository's `AGENTS.md`, after user approval |
| Most sessions, whatever the task | Omega `AGENTS.md`, after user approval, staying under 4400 bytes |

Skill sources live in the Omega repository (`~/.agents/skills/*` are links to it). When a skill gets
its first lesson, add one line to its `SKILL.md`: `Read references/lessons.md before acting.`

## 4. Apply deltas, never rewrites

One line per lesson: `- [YYYY-MM-DD n=1] When <situation>, <action>. (ref: <task or evidence>)`

- Add: a new line.
- Merge: same trigger seen again, increment `n` and update the date.
- Prune: a lesson contradicted by evidence, covered by a tool or hook, or still `n=1` after 60 days.
- At most 20 lines per file. When full, prune the oldest lowest-`n` line before adding.
- A lesson that a hook or tool can enforce belongs in the code: propose that change instead.

## 5. Validate and report

- `'Ω-run' --validation -- python3 -m pytest tools/test_response_settings.py` from the Omega checkout.
- Reply with one line per delta (`add|merge|prune`, file, lesson). No commit without authorization.
