from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from automesh.io import mesh_to_obj_string, save_obj
from automesh.mesh import Mesh
from automesh.qem import CollapseCandidate


@dataclass(frozen=True)
class RewardContext:
    original: Mesh
    previous: Mesh
    current: Mesh
    candidate: CollapseCandidate
    step_index: int
    target_faces: int
    done: bool


class RewardProvider(Protocol):
    def __call__(self, context: RewardContext) -> float:
        ...


class CompositeReward:
    def __init__(self, providers: list[RewardProvider]) -> None:
        self.providers = providers

    def __call__(self, context: RewardContext) -> float:
        return float(sum(provider(context) for provider in self.providers))


class QEMCostReward:
    """Dense penalty for expensive collapses."""

    def __init__(self, weight: float = 1.0) -> None:
        self.weight = weight

    def __call__(self, context: RewardContext) -> float:
        return -self.weight * float(context.candidate.cost)


class FaceBudgetReward:
    """Small positive reward for making progress toward the target face count."""

    def __init__(self, progress_weight: float = 0.01, completion_bonus: float = 1.0) -> None:
        self.progress_weight = progress_weight
        self.completion_bonus = completion_bonus

    def __call__(self, context: RewardContext) -> float:
        removed = max(0, context.previous.face_count - context.current.face_count)
        reward = self.progress_weight * removed
        if context.done and context.current.face_count <= context.target_faces:
            reward += self.completion_bonus
        return float(reward)


class ChamferProxyReward:
    """Sparse geometric proxy for render similarity.

    This is not a substitute for a renderer. It gives the first RL loop a cheap,
    deterministic signal while nvdiffrast/Unreal reward providers are still absent.
    """

    def __init__(self, weight: float = 1.0, interval: int = 10) -> None:
        self.weight = weight
        self.interval = max(1, interval)

    def __call__(self, context: RewardContext) -> float:
        if not context.done and context.step_index % self.interval != 0:
            return 0.0
        source = representative_points(context.original)
        target = representative_points(context.current)
        if len(source) == 0 or len(target) == 0:
            return -self.weight * 1e3
        return -self.weight * symmetric_chamfer(source, target)


class PhotorealRewardStub:
    """Placeholder for sparse Unreal/Mitsuba/Blender reward integration."""

    def __init__(self, interval: int = 100, weight: float = 1.0) -> None:
        self.interval = max(1, interval)
        self.weight = weight

    def __call__(self, context: RewardContext) -> float:
        if not context.done and context.step_index % self.interval != 0:
            return 0.0
        return 0.0


class ExternalCommandReward:
    """Sparse reward from an external renderer/evaluator command.

    The command receives formatted paths for the original and current mesh and
    must print a single float reward to stdout. This keeps Unreal/Mitsuba/Blender
    integration outside the training loop until that bridge is ready.
    """

    def __init__(
        self,
        command: list[str],
        interval: int = 100,
        weight: float = 1.0,
        timeout_seconds: float = 300.0,
        work_dir: str | Path | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.interval = max(1, interval)
        self.weight = weight
        self.timeout_seconds = timeout_seconds
        self.work_dir = Path(work_dir) if work_dir is not None else None

    def __call__(self, context: RewardContext) -> float:
        if not context.done and context.step_index % self.interval != 0:
            return 0.0

        with tempfile.TemporaryDirectory(prefix="automesh_reward_") as tmp:
            tmp_path = Path(tmp)
            original_path = tmp_path / "original.obj"
            current_path = tmp_path / "current.obj"
            save_obj(context.original, original_path)
            save_obj(context.current, current_path)
            command = [
                part.format(
                    original=original_path,
                    current=current_path,
                    step=context.step_index,
                    faces=context.current.face_count,
                )
                for part in self.command
            ]
            completed = subprocess.run(
                command,
                cwd=self.work_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        try:
            return self.weight * float(completed.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise ValueError("external reward command must print a float reward") from exc


class HttpRenderReward:
    """Sparse reward from a *persistent* render service over HTTP.

    Unlike `ExternalCommandReward` (which cold-starts a subprocess per call),
    this client assumes a long-running renderer -- e.g. a headless Unreal
    process listening on localhost -- that stays warm across calls. Each
    invocation is a single HTTP request, so GPU state, shaders, and camera
    setup are reused. Meshes are serialized into the request body as OBJ text,
    so the service needs no shared filesystem access.

    Contract with the service:

        POST <endpoint>
        Content-Type: application/json
        {"original_obj": "v ...\nf ...\n", "current_obj": "v ...\nf ...\n",
         "step": 123, "faces": 456}

        200 OK
        Content-Type: application/json
        {"reward": 0.83}

    A non-2xx response or malformed body raises and aborts the training step,
    matching the strict-failure semantics of `ExternalCommandReward`.
    """

    def __init__(
        self,
        endpoint: str,
        interval: int = 100,
        weight: float = 1.0,
        timeout_seconds: float = 30.0,
        user_agent: str = "automesh-http-render-reward/0.1",
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint must not be empty")
        self.endpoint = endpoint
        self.interval = max(1, interval)
        self.weight = weight
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def __call__(self, context: RewardContext) -> float:
        if not context.done and context.step_index % self.interval != 0:
            return 0.0

        payload = {
            "original_obj": mesh_to_obj_string(context.original),
            "current_obj": mesh_to_obj_string(context.current),
            "step": context.step_index,
            "faces": context.current.face_count,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if 200 <= resp.status < 300:
                    body = resp.read()
                else:
                    raise RuntimeError(f"render service returned HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"render service returned HTTP {exc.code}") from exc
        parsed = json.loads(body.decode("utf-8"))
        reward = float(parsed["reward"])

        return self.weight * reward


def render_mesh_image(
    mesh: Mesh,
    endpoint: str = "http://127.0.0.1:8765/render",
    width: int = 1024,
    height: int = 1024,
    timeout_seconds: float = 30.0,
    user_agent: str = "automesh-http-render-reward/0.1",
) -> bytes:
    """Render a single mesh to a PNG via the persistent UE render service.

    Mirrors the `HttpRenderReward` transport: serialize the mesh to OBJ text,
    POST it to `/render`, and return the raw PNG bytes. On a non-2xx response
    or a JSON error body this raises `RuntimeError`.
    """
    payload = {
        "obj": mesh_to_obj_string(mesh),
        "width": int(width),
        "height": int(height),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            if not (200 <= resp.status < 300):
                raise RuntimeError(f"render service returned HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"render service returned HTTP {exc.code}") from exc
    # The service returns image/png on success and application/json on error.
    if "application/json" in ctype:
        try:
            err = json.loads(body.decode("utf-8")).get("error", "render failed")
        except (ValueError, UnicodeDecodeError):
            err = "render failed"
        raise RuntimeError(f"render service error: {err}")
    return body


def default_reward() -> RewardProvider:
    return CompositeReward(
        [
            QEMCostReward(weight=1.0),
            FaceBudgetReward(progress_weight=0.02, completion_bonus=1.0),
            ChamferProxyReward(weight=0.1, interval=10),
        ]
    )


def representative_points(mesh: Mesh) -> np.ndarray:
    if mesh.face_count == 0:
        return mesh.vertices
    tris = mesh.face_vertices()
    centroids = tris.mean(axis=1)
    return np.vstack([mesh.vertices, centroids])


def symmetric_chamfer(a: np.ndarray, b: np.ndarray) -> float:
    d2 = pairwise_squared_distances(a, b)
    return float(np.mean(np.min(d2, axis=1)) + np.mean(np.min(d2, axis=0)))


def pairwise_squared_distances(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    diff = a[:, None, :] - b[None, :, :]
    return np.sum(diff * diff, axis=-1)
