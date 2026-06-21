from __future__ import annotations

import random
from typing import Protocol

from automesh.env import MeshSimplificationEnv


class Policy(Protocol):
    def choose_action(self, env: MeshSimplificationEnv) -> int:
        ...


class GreedyQEMPolicy:
    def choose_action(self, env: MeshSimplificationEnv) -> int:
        if env.legal_action_count() == 0:
            raise ValueError("no legal actions")
        return 0


class RandomTopKPolicy:
    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)

    def choose_action(self, env: MeshSimplificationEnv) -> int:
        action_count = env.legal_action_count()
        if action_count == 0:
            raise ValueError("no legal actions")
        return self.rng.randrange(action_count)


def make_policy(name: str, seed: int | None = None) -> Policy:
    if name == "qem-greedy":
        return GreedyQEMPolicy()
    if name == "random":
        return RandomTopKPolicy(seed=seed)
    raise ValueError(f"unknown policy: {name}")
