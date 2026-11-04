---
name: cpp-quality
description: Reviews or changes robotics C++ with attention to ownership, concurrency and realtime behavior. Use for C++ implementation or review, not Python-only tasks.
---

# C++ quality

Identify the owning component, configured C++ standard, existing interfaces and the affected thread before editing. Reuse project abstractions; prefer the smallest readable change and RAII with explicit ownership. Check reference/view lifetime, move semantics, exception guarantees and resource cleanup at the actual call sites.

For concurrent code, identify who owns each mutable state, lock ordering, callback lifetime, shutdown/join behavior and cancellation. Do not infer thread safety from a unit test or add atomics without a synchronization contract.

For control loops, preserve frequency and deadlines. Keep unbounded waits, filesystem/network I/O, logging, heap allocation and destruction with unpredictable cost out of the realtime path. Measure allocations and timing when changing that path; do not claim realtime safety from style alone.

Preserve units, frames, signs, joint ordering and actuator limits. Use the project formatter and existing targeted test/build commands through captured execution. Return the exact command, exit status and evidence path. Add a regression test only for meaningful changed behavior; keep unrelated cleanup separate.
