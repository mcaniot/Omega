---
name: robotics-validation
description: Use when choosing or running checks to prove a robotics behavior change (motion, control, policy, simulation), or judging whether unit, smoke or simulation evidence is sufficient. Not for generic non-robotics tests.
---

# Robotics validation

State the behavior and acceptance criterion before choosing a test. Establish the robot/backend, units, frames, joint order, observation/action shapes, normalization, frequency and limits from project sources. Preserve sim/hardware semantics; physical robot commands require explicit authorization.

Use the cheapest check that can detect the regression:
1. Static/configuration/import checks.
2. Targeted Python/C++ unit tests at the behavior owner.
3. Minimal bounded headless smoke simulation.
4. Relevant integration simulation.
5. Full validation only when the remaining risk requires it.

Stop at a failed prerequisite. Do not launch a simulation to work around a known import/configuration failure. Keep the exact revision/diff, environment, seed, device, backend, timestep and configuration with each result. Capture output externally and reference artifacts, never paste logs into task state.

For motion, inspect actual joint limits, forbidden contacts, finite states and control stability over the relevant interval. For policies, check temporal chunks/horizons as well as action/observation ordering. Assert observable outcomes with meaningful negative controls; unit tests do not certify physical safety.

Reuse successful checks until affected code, dependencies or configuration change. Isaac Lab already supplies repository skills for test audits and backend/asset diagnostics; load only the matching one without copying it globally. Report PASS, FAIL, BLOCKED or INCONCLUSIVE with exact commands, exit statuses and evidence references; distinguish skipped or unavailable validation from success.
