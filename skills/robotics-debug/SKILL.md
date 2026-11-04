---
name: robotics-debug
description: Reproduces and diagnoses failures in robotic controllers, QP solvers, Zenoh, threading or Isaac Lab physics. Use for a concrete failure or unstable behavior.
---

# Robotics debugging

First reproduce the reported failure with the smallest bounded case. Record revision/diff, interpreter, backend, device, seed, configuration, timing and exact command. Capture complete output externally; inspect bounded error/source excerpts and retain full stack traces.

Separate observations from hypotheses. Trace the failing value to its owner before changing code. Check units, frames, joint/action ordering, tensor shapes, timestamps and normalization against the project contract.

Choose diagnostics for the failure:
- C++/threads: lifetimes, races, lock ordering, callback ownership and shutdown; use an available sanitizer or debugger on a bounded reproduction.
- QP: solver status, dimensions, finite inputs, residuals, feasibility, scaling and active constraints. Do not loosen safety limits to make it converge.
- Zenoh: endpoint/session configuration, delivery semantics, sample age, callback scheduling and serialization.
- Isaac Lab/PhysX/Newton: selected backend and imported source, timestep/substeps, contact configuration, joint axes/order and asset conversion. Load only the relevant repository skill, such as diagnosing-joint-poses or setup-troubleshooting; keep its canonical source in the repository.

Test one evidence-backed hypothesis at a time. Do not repeat an unchanged failure. After two distinct failed approaches to the same blocker, checkpoint evidence and report the blocker. Verify the correction at the owning boundary; never substitute a plausible explanation for reproduction evidence.
