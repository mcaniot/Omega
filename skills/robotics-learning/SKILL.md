---
name: robotics-learning
description: Robot learning (RL, imitation, VLA policies) and physics simulation (Isaac Lab, Newton, MuJoCo, Gazebo) — environments, rewards, observation/action spaces, training/eval scripts, datasets and sim-to-real. Use when that semantics is central, not for routine robotics code.
---

# Robot learning and simulation

Establish the contract from project sources before changing or judging anything: robot/asset, backend and version, physics dt, decimation and control rate, observation/action layout, order, units, normalization and clipping, joint order, frames, limits and episode length. State any required contract change explicitly; never change it silently.

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

Validate with the cheapest discriminating check (config/shape assertions, single-env step, short headless rollout) before training; load `robotics-validation` to choose further checks and `robotics-debug` for a concrete failure. Use repository-provided Isaac Lab skills when present instead of copying them.
