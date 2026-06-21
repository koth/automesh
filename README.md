# automesh

Research prototype for policy-guided QEM mesh simplification.

The first milestone is intentionally small:

1. Build a legal edge-collapse environment around QEM.
2. Expose the top-K QEM candidates as an RL action space.
3. Keep reward providers pluggable, so fast geometry/render proxies can be used during training and a photoreal renderer can be added later as a sparse evaluator.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Run

Create a procedural cube and simplify it with greedy QEM:

```bash
automesh demo --steps 4
```

Simplify an OBJ:

```bash
automesh simplify input.obj output.obj --target-faces 1000 --policy qem-greedy --top-k 16
```

The current implementation is a research scaffold, not a production decimator. It rebuilds topology after each collapse for clarity. Once the reward loop is validated, the collapse backend can be swapped for a half-edge implementation.
