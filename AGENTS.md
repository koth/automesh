# Repository Guidelines

A contributor guide for **automesh**, a research prototype for policy-guided QEM mesh simplification. Use this document as the entry point for onboarding, day-to-day development, and review.

## Project Structure & Module Organization

The package is a small Python research scaffold. Layout:

- `src/automesh/` — package source, installed as `automesh`.
  - `mesh.py` — `Mesh` data type and procedural helpers (e.g. `Mesh.cube()`).
  - `qem.py` — Quadric Error Metrics implementation and top-K candidate enumeration.
  - `env.py` — Gym-like `MeshSimplificationEnv` (reset/step API, legal actions).
  - `policies.py` — pluggable policies; current baselines are `qem-greedy` and `random`.
  - `rewards.py` — reward providers (`HttpRenderReward`, `ExternalCommandReward`, geometry proxies) plus `render_mesh_image` for the `/render` route; all serialize meshes as OBJ text in the body — no shared filesystem.
  - `io.py` — Wavefront OBJ loader/saver, plus `mesh_to_obj_string`/`mesh_from_obj_string` for in-memory serialization used by the HTTP paths.
  - `cli.py` — `automesh` console entry point (`demo`, `simplify`, `render-image` subcommands).
- `tests/` — pytest suite: `test_qem_env.py`, `test_io.py`, `test_external_reward.py`.
- `docs/ROADMAP.md` — milestone plan (M1–M5); `docs/UNREAL_SETUP.md` + `docs/AUTOMESH_RENDER_BUILD.md` — UE render-service setup/build.
- `ue/AutoMeshRender/` — UE5 persistent HTTP render service (Editor target, `IHttpRouter`): `/reward` (6-view depth MSE) and `/render` (single 3/4 view → PNG). Run uncooked via `UnrealEditor -game -RenderOffScreen`.
- `scripts/sample_thingi10k.py` — Thingi10K asset sampler for test meshes.
- `datas/` — datasets (`Thingi10K.tar.gz`); `out/` — generated artefacts (both gitignored).
- `pyproject.toml` — build metadata, dependencies, and pytest config.

## Build, Test, and Development Commands

Create the dev environment once, then use these from the repo root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest            # run the full test suite (testpaths configured in pyproject.toml)
automesh demo --steps 4
automesh simplify input.obj output.obj --target-faces 1000 --policy qem-greedy --top-k 16
# render a mesh to PNG via the UE render service (/render route):
automesh render-image input.obj out.png --endpoint http://127.0.0.1:8765/render
```

`pip install -e ".[dev]"` exposes the `automesh` console script and pulls in `pytest`. CLI outputs land in `out/` by convention.

## Coding Style & Naming Conventions

- Python ≥ 3.11, `from __future__ import annotations` in every module.
- 4-space indentation, type hints on public functions, `numpy.ndarray` for geometry.
- Modules and files: `snake_case` (`qem.py`, `external_reward.py`); classes `PascalCase` (`MeshSimplificationEnv`); functions/variables `snake_case`; constants `UPPER_SNAKE_CASE`.
- Public policies are registered by string name (`qem-greedy`, `random`) — keep CLI choices in sync with `policies.make_policy`.
- No formatter or linter is pinned yet; match surrounding code. Keep imports grouped: stdlib, third-party, local.

## Testing Guidelines

- Framework: `pytest` with `testpaths = ["tests"]` and `pythonpath = ["src"]` (no install required to run).
- Name test files `test_<module>.py` and test functions `test_<behavior>`.
- Cover the public surface: env transitions, OBJ round-trips, and reward providers (including shell-out edge cases).
- Run a single file with `pytest tests/test_qem_env.py -q`. Add a test alongside any non-trivial change.

## Commit & Pull Request Guidelines

- Commits: short imperative subject (≤72 chars), e.g. `Add random policy baseline`. Group related changes; avoid mixing refactors with feature work.
- Pull requests: describe motivation and approach, link issues or roadmap milestones, list CLI/test commands run, and attach before/after artefacts (mesh stats, `out/*.obj`) when behaviour changes. Keep diffs focused and PRs reviewable in one sitting.

## Agent-Specific Instructions

- Rebuild topology after each collapse is intentional for clarity — do not switch to half-edge without a roadmap note.
- Keep reward providers pluggable; visual rewards must stay out of the inner training loop. Prefer the verified `HttpRenderReward` + persistent UE service pattern (`ue/AutoMeshRender/`, see `docs/UNREAL_SETUP.md`) over per-call subprocesses; `ExternalCommandReward` remains for one-shot local renderers only.
- Generated OBJ files belong in `out/` (gitignored); never commit them to `src/`.
