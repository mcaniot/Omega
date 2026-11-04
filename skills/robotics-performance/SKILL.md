---
name: robotics-performance
description: Use when the task concerns latency, control-loop jitter or deadline misses, profiling, simulation/rendering/camera throughput, or realtime optimization of robotics code.
---

# Robotics performance

Define the metric and correctness constraints before optimizing. Measure the existing implementation with a bounded representative workload. Record hardware, device, backend, revision/diff, configuration, seed, environment count, timestep, render/camera settings and sample count.

For control paths, measure deadline misses, latency and jitter distributions (p50/p95/p99), allocations, synchronization and blocking. State the jitter definition and clock source. For simulation/cameras, separate physics, sensor generation, rendering, readback and recording; distinguish wall-clock throughput from simulated time.

Separate initialization/JIT/warmup from steady state. Account for GPU asynchrony with suitable device events or synchronization at measurement boundaries; do not accidentally benchmark submission time. Preserve raw samples and profiler artifacts externally and return compact statistics with evidence paths.

Change one demonstrated bottleneck with the smallest maintainable implementation. Compare before/after using the same workload, warmup, hardware and settings; include repeated samples and variability when meaningful. Run the relevant correctness check after optimization. Never trade away collision checks, limits, fidelity or control timing without explicit task authorization.

If comparable runs or reliable metrics are unavailable, report the limitation and avoid improvement percentages. End an expensive campaign when its hypothesis is resolved; escalate only when cheaper measurements leave a consequential question unanswered.
