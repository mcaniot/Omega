---
name: systematic-debugging
description: Use when a bug, crash, failing or flaky test, regression or unstable behavior has a root cause that is not obvious from the error, or when root-cause analysis is requested. Includes C++ threading, QP solver, Zenoh and Isaac Lab/PhysX/Newton failures.
---

# Systematic debugging

No fix before the root cause is established.

1. Reproduce the failure with the smallest bounded case. Record revision/diff, interpreter, backend, device, seed, configuration, timing and exact command. Capture complete output externally; read the full error and stack trace from evidence.
2. Compare: check recent changes and the nearest working version or similar working code; list the differences. When a dependency owns the failing behavior, read its source at the installed version and its upstream issues/changelog before hypothesizing. Trace the failing value to its owner, adding diagnostics at component boundaries. Check units, frames, joint/action ordering, tensor shapes, timestamps and normalization against the project contract.
3. Test one evidence-backed hypothesis at a time with the smallest change; separate observations from hypotheses. A disproven hypothesis is replaced, never stacked with more fixes.
4. Fix at the owner: add a test that fails before the fix, apply one correction, and verify it at the owning boundary. Symptom patches and "try X and see" are failures.

Flaky or timing-dependent: measure the failure rate over bounded repeated runs; replace arbitrary sleeps with waits on the actual condition, with a timeout.

Repeated physical failure on video: give a fresh worker only the rollout video (a contact sheet if it cannot read video), without code or expectations, and ask what happened; only then give another worker the videos, code and measurements to ask what to change.

Domain diagnostics:
- C++/threads: lifetimes, races, lock ordering, callback ownership and shutdown; use an available sanitizer or debugger on a bounded reproduction.
- QP: solver status, dimensions, finite inputs, residuals, feasibility, scaling and active constraints. Do not loosen safety limits to make it converge.
- Zenoh: endpoint/session configuration, delivery semantics, sample age, callback scheduling and serialization.
- Isaac Lab/PhysX/Newton: selected backend and imported source, timestep/substeps, contact configuration, joint axes/order and asset conversion; with Newton's MuJoCo solver, export the compiled model (`save_to_mjcf`) and read it before a hypothesis on contacts, masses or gains. Load only the relevant repository skill, such as diagnosing-joint-poses or setup-troubleshooting.

Do not repeat an unchanged failure. After two distinct failed approaches, or when each fix reveals a new problem elsewhere, stop: checkpoint evidence and escalate the design question to `Plan` instead of a third patch. Never substitute a plausible explanation for reproduction evidence.

Read references/lessons.md before acting.
