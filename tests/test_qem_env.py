from automesh.env import MeshSimplificationEnv
from automesh.mesh import Mesh
from automesh.qem import QEMSimplifier


def test_qem_candidates_exist_for_cube() -> None:
    mesh = Mesh.cube()
    candidates = QEMSimplifier().candidates(mesh, top_k=4)

    assert candidates
    assert len(candidates) <= 4
    assert candidates[0].cost <= candidates[-1].cost


def test_collapse_reduces_cube_faces() -> None:
    mesh = Mesh.cube()
    simplifier = QEMSimplifier()
    candidate = simplifier.candidates(mesh, top_k=1)[0]

    result = simplifier.collapse(mesh, candidate)

    assert result.mesh.face_count < mesh.face_count
    assert result.mesh.vertex_count < mesh.vertex_count


def test_env_rollout_reaches_target() -> None:
    env = MeshSimplificationEnv(Mesh.cube(), target_faces=4, top_k=8)
    observation = env.reset()

    assert observation["face_count"] == 12

    terminated = False
    for _ in range(10):
        result = env.step(0)
        terminated = result.terminated
        if terminated:
            break

    assert terminated
    assert env.current.face_count <= 4
