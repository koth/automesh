# Research Roadmap

## Goal

Learn an edge-collapse policy that improves QEM-style simplification under render-aware visual objectives, while keeping inference fast enough for batch asset processing.

## Milestone 1: QEM Environment

- Triangle mesh IO.
- Legal edge-collapse backend.
- QEM top-K candidate generator.
- Gym-like environment.
- Greedy and random policy baselines.
- Geometry proxy rewards.

Status: implemented as the initial scaffold.

## Milestone 2: Fast Render-Aware Reward

- Add nvdiffrast or PyTorch3D renderer.
- Render original and simplified meshes from fixed multi-view cameras.
- Compute silhouette, depth, normal, RGB, and perceptual losses.
- Use these losses as dense or periodic rewards.

Expected result: policy learns to preserve visible silhouettes and high-frequency appearance better than vanilla QEM.

## Milestone 3: Learnable Policy

- Represent each candidate edge with local geometric features and visibility features.
- Start with a simple MLP over top-K candidates.
- Move to a graph policy with local neighborhoods once the loop is stable.
- Train with PPO/A2C or behavior cloning from improved rollouts.

Expected result: a reusable policy that chooses among QEM candidates without per-asset expensive search.

## Milestone 4: Sparse Photoreal Reward

- Use `ExternalCommandReward` to call Unreal, Mitsuba, or Blender at sparse intervals.
- Keep the renderer out of the inner loop.
- Distill photoreal evaluations into a faster visual-cost model.

Expected result: improved correlation with production renderer quality without making inference renderer-dependent.

## Milestone 5: Paper-Grade Evaluation

- Baselines: QEM, attribute-aware QEM, modern textured simplifiers, Neural Mesh Simplification where applicable.
- Metrics: Chamfer, Hausdorff proxy, normal deviation, silhouette IoU, image-space LPIPS/L1, face budget, runtime.
- Datasets: Thingi10K subset, textured assets, generated/reconstructed non-manifold meshes.
- Ablations: QEM-only, RL-only, render reward only, sparse photoreal reward, distilled visual cost.
