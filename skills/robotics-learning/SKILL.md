---
name: robotics-learning
description: Use when the task involves Isaac Lab, reinforcement learning, imitation learning, VLA or other learned robot policies — environments, rewards, observation/action spaces, training/evaluation, datasets or sim-to-real. Not for routine robotics code without learning or simulation semantics.
---

# Robot learning and simulation

Establish the contract from project sources before changing or judging anything: robot/asset, backend and version, physics dt, decimation and control rate, observation/action layout, order, units, normalization and clipping, joint order, frames, limits and episode length. State any required contract change explicitly; never change it silently.

Before writing a new reward or observation term, randomization, algorithm, controller or dataset transform, look for an existing equivalent in the installed framework source (Isaac Lab tasks and mdp terms, the RL/IL library) and in the reference paper or implementation of the method; adapt it and state every deviation from the reference.

Environments (Isaac Lab and similar):
- Identify the workflow (manager-based or direct) and the owning term/config; change behavior there, not in wrappers.
- Distinguish terminated (task outcome) from truncated (time-out); bootstrapping and success metrics depend on it.
- Check vectorized shapes `(num_envs, ...)`, per-env reset paths (buffers, targets, history, randomization) and device placement.
- Rewards: list each term with weight, scale and dt dependence; look for sparse/shaped imbalance, exploitable shaping, termination that removes future reward and penalties that dominate learning.

Training and evaluation:
- Diagnose from logged metrics (per-term rewards, episode length, success rate, value/entropy/KL, action saturation), not intuition. Check rewards, terminations, seeding and shapes before blaming the algorithm.
- Keep train/eval parity: same observation pipeline, normalization statistics, action scaling, control rate and checkpoint. Evaluate several seeds and report variance.

Imitation and datasets: verify timestamp alignment, FPS, action horizon/chunking, observation/action ordering, normalization statistics and the frame convention between recording, training and deployment.

Sim-to-real: ground domain-randomization ranges in measurements; model actuation latency, action delay, actuator limits, sensor noise and control-rate jitter; keep safety limits and e-stop semantics identical to hardware. Simulation success never certifies hardware behavior; hardware runs need explicit authorization.

Validate with the cheapest discriminating check (config/shape assertions, single-env step, short headless rollout) before training; load `robotics-validation` to choose further checks and `systematic-debugging` for a concrete failure. Use repository-provided Isaac Lab skills when present instead of copying them.
