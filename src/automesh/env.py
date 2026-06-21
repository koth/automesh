from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from automesh.mesh import Mesh
from automesh.qem import CollapseCandidate, QEMSimplifier
from automesh.rewards import RewardContext, RewardProvider, default_reward


@dataclass(frozen=True)
class StepResult:
    observation: dict[str, Any]
    reward: float
    terminated: bool
    truncated: bool
    info: dict[str, Any]


class MeshSimplificationEnv:
    """Small Gym-like environment for policy-guided QEM simplification."""

    def __init__(
        self,
        mesh: Mesh,
        target_faces: int | None = None,
        target_ratio: float | None = None,
        top_k: int = 16,
        max_steps: int | None = None,
        simplifier: QEMSimplifier | None = None,
        reward: RewardProvider | None = None,
    ) -> None:
        if target_faces is None:
            if target_ratio is None:
                target_ratio = 0.5
            target_faces = max(1, int(round(mesh.face_count * target_ratio)))
        if target_faces < 1:
            raise ValueError("target_faces must be positive")
        if target_faces >= mesh.face_count:
            raise ValueError("target_faces must be smaller than the initial face count")
        self.original = mesh.copy()
        self.target_faces = int(target_faces)
        self.top_k = int(top_k)
        self.max_steps = max_steps
        self.simplifier = simplifier or QEMSimplifier()
        self.reward_provider = reward or default_reward()
        self.current = mesh.copy()
        self.step_index = 0
        self._candidates: list[CollapseCandidate] = []

    def reset(self) -> dict[str, Any]:
        self.current = self.original.copy()
        self.step_index = 0
        self._refresh_candidates()
        return self._observation()

    def step(self, action: int) -> StepResult:
        if not self._candidates:
            return StepResult(self._observation(), 0.0, True, False, {"reason": "no_candidates"})
        if action < 0 or action >= len(self._candidates):
            raise ValueError(f"action must be in [0, {len(self._candidates) - 1}]")

        previous = self.current
        candidate = self._candidates[action]
        result = self.simplifier.collapse(previous, candidate)
        self.current = result.mesh
        self.step_index += 1

        terminated = self.current.face_count <= self.target_faces
        truncated = self.max_steps is not None and self.step_index >= self.max_steps
        self._refresh_candidates()
        context = RewardContext(
            original=self.original,
            previous=previous,
            current=self.current,
            candidate=candidate,
            step_index=self.step_index,
            target_faces=self.target_faces,
            done=terminated or truncated,
        )
        reward = self.reward_provider(context)
        return StepResult(
            observation=self._observation(),
            reward=reward,
            terminated=terminated,
            truncated=bool(truncated),
            info={
                "edge": candidate.edge,
                "cost": candidate.cost,
                "faces": self.current.face_count,
                "vertices": self.current.vertex_count,
            },
        )

    def legal_action_count(self) -> int:
        return len(self._candidates)

    def candidates(self) -> list[CollapseCandidate]:
        return list(self._candidates)

    def _refresh_candidates(self) -> None:
        if self.current.face_count <= self.target_faces:
            self._candidates = []
        else:
            self._candidates = self.simplifier.candidates(self.current, top_k=self.top_k)

    def _observation(self) -> dict[str, Any]:
        if self._candidates:
            candidate_features = np.stack([candidate.features for candidate in self._candidates], axis=0)
            candidate_costs = np.array([candidate.cost for candidate in self._candidates], dtype=np.float64)
        else:
            candidate_features = np.empty((0, 5), dtype=np.float64)
            candidate_costs = np.empty((0,), dtype=np.float64)
        return {
            "vertices": self.current.vertices,
            "faces": self.current.faces,
            "candidate_features": candidate_features,
            "candidate_costs": candidate_costs,
            "face_count": self.current.face_count,
            "vertex_count": self.current.vertex_count,
            "target_faces": self.target_faces,
            "progress": 1.0 - (self.current.face_count - self.target_faces)
            / max(1, self.original.face_count - self.target_faces),
        }
