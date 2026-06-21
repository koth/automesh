import sys

from automesh.mesh import Mesh
from automesh.qem import QEMSimplifier
from automesh.rewards import ExternalCommandReward, RewardContext


def test_external_command_reward_reads_float() -> None:
    mesh = Mesh.cube()
    simplifier = QEMSimplifier()
    candidate = simplifier.candidates(mesh, top_k=1)[0]
    current = simplifier.collapse(mesh, candidate).mesh
    reward = ExternalCommandReward(
        command=[sys.executable, "-c", "print(0.25)"],
        interval=1,
    )

    value = reward(
        RewardContext(
            original=mesh,
            previous=mesh,
            current=current,
            candidate=candidate,
            step_index=1,
            target_faces=4,
            done=False,
        )
    )

    assert value == 0.25
