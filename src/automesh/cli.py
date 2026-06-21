from __future__ import annotations

import argparse
from pathlib import Path

from automesh.env import MeshSimplificationEnv
from automesh.io import load_obj, save_obj
from automesh.mesh import Mesh
from automesh.policies import make_policy


def main() -> None:
    parser = argparse.ArgumentParser(prog="automesh")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run a small procedural simplification demo")
    demo.add_argument("--steps", type=int, default=6)
    demo.add_argument("--top-k", type=int, default=8)
    demo.add_argument("--policy", choices=["qem-greedy", "random"], default="qem-greedy")

    simplify = subparsers.add_parser("simplify", help="simplify a Wavefront OBJ")
    simplify.add_argument("input", type=Path)
    simplify.add_argument("output", type=Path)
    simplify.add_argument("--target-faces", type=int, default=None)
    simplify.add_argument("--target-ratio", type=float, default=0.5)
    simplify.add_argument("--top-k", type=int, default=16)
    simplify.add_argument("--policy", choices=["qem-greedy", "random"], default="qem-greedy")
    simplify.add_argument("--max-steps", type=int, default=None)
    simplify.add_argument("--seed", type=int, default=0)

    args = parser.parse_args()
    if args.command == "demo":
        run_demo(args)
    elif args.command == "simplify":
        run_simplify(args)


def run_demo(args: argparse.Namespace) -> None:
    mesh = Mesh.cube()
    env = MeshSimplificationEnv(mesh, target_faces=4, top_k=args.top_k, max_steps=args.steps)
    observation = env.reset()
    print_summary("initial", observation)
    rollout(env, policy=args.policy, max_steps=args.steps)


def run_simplify(args: argparse.Namespace) -> None:
    mesh = load_obj(args.input)
    env = MeshSimplificationEnv(
        mesh,
        target_faces=args.target_faces,
        target_ratio=args.target_ratio,
        top_k=args.top_k,
        max_steps=args.max_steps,
    )
    observation = env.reset()
    print_summary("initial", observation)
    rollout(env, policy=args.policy, max_steps=args.max_steps, seed=args.seed)
    save_obj(env.current, args.output)
    print(f"saved {args.output}")


def rollout(env: MeshSimplificationEnv, policy: str, max_steps: int | None, seed: int | None = None) -> None:
    policy_impl = make_policy(policy, seed=seed)
    steps = 0
    while env.legal_action_count() > 0:
        if max_steps is not None and steps >= max_steps:
            break
        action = policy_impl.choose_action(env)
        result = env.step(action)
        steps += 1
        edge = result.info.get("edge")
        print(
            "step={step} action={action} edge={edge} reward={reward:.6g} "
            "faces={faces} vertices={vertices}".format(
                step=steps,
                action=action,
                edge=edge,
                reward=result.reward,
                faces=result.info.get("faces"),
                vertices=result.info.get("vertices"),
            )
        )
        if result.terminated or result.truncated:
            break

def print_summary(label: str, observation: dict) -> None:
    print(
        f"{label}: faces={observation['face_count']} "
        f"vertices={observation['vertex_count']} target={observation['target_faces']}"
    )


if __name__ == "__main__":
    main()
