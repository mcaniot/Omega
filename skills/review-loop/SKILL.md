---
name: review-loop
description: Use when the user asks to review their own code in a loop, fixing bugs, reducing lines and simplifying reading until no finding remains (review-fix loop, "loop until clean").
---

# Review-fix-simplify loop

Goals, in priority order: fix bugs > fewer lines > easier reading. Behaviour stays identical except for bug fixes. For reviewing someone else's MR/PR or drafting comments, use `code-review` instead.

1. Scope: `git status --short --branch`; target = diff against the base branch plus uncommitted changes, or the files the user names; a named branch that is the base or the current branch means its whole tree. Record the baseline line count of the target files and the test/lint status. No tests covering the target: write characterization tests first, or limit the loop to bugs.
2. Each round, one fresh `Plan` agent with `model: fable`, read-only. Brief: target paths, test/lint commands, findings already rejected (do not re-raise). Role: senior engineer who finds
   - bugs: logic, edge cases, error handling, resources, concurrency, units/frames/shapes;
   - reductions: dead code, duplication, needless abstraction, wrappers or variables, reimplemented stdlib or dependency features, deep nesting (early return, guard clauses);
   - readability: unclear names, overly long functions, tangled control flow, comments that restate code.
   Output per finding: `file:line | bug/reduction/readability | confidence 0-100 | evidence | concrete patch | estimated lines saved`.
   Forbidden: code golf, dense one-liners, clever tricks, public API or confirmed-contract changes, removing validation, safety or error handling.
3. Triage in the main session: re-check each finding in the code. Keep bugs with confidence ≥ 80; keep reductions and readability changes only if behaviour is unchanged and the result reads at least as easily. Record the reason for each rejection.
4. Apply: bugs first (add a regression test when missing), then simplifications. Project formatter, lint, tests; on failure fix it or revert that change.
5. Measure: `git diff --numstat` against the baseline gives the net lines removed this round.
6. Stop when no bug remains and the round removed fewer than 5 lines, after 4 rounds, or when a finding comes back after being fixed or rejected (oscillation: report it to the user).
7. Report: bugs fixed, lines before → after, simplifications applied, findings rejected with reasons, final test status. No commit without authorization.
