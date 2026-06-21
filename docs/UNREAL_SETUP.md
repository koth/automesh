# Unreal Engine Reward Setup (Ubuntu, headless, NVIDIA 8-16GB)

Goal: run a **persistent headless UE5 render service** on the training box,
and call it from `HttpRenderReward` in `src/automesh/rewards.py` over HTTP as
the sparse photoreal reward (Roadmap M4). The service is implemented in
`ue/AutoMeshRender/` (in this repo).

The contract between client and service:

- **Request**: `POST <endpoint>`, `Content-Type: application/json`,
  body `{"original_obj": <obj text>, "current_obj": <obj text>, "step": int, "faces": int}`.
  Meshes are serialized as OBJ text in the body — no shared filesystem needed.
- **Response**: `200` with `{"reward": <float>}`. A non-2xx or malformed body
  raises and aborts the training step.
- **Mesh scope**: only the two OBJ documents are exchanged. Cameras, materials,
  lighting, and the loss live entirely in the UE service.
- **Persistence**: the service stays warm across calls; each reward is one HTTP
  request (tens of ms), not a process cold start. This is why we use
  `HttpRenderReward`, not `ExternalCommandReward` (subprocess per call).

> Scope note: nvdiffrast/PyTorch3D for the dense render-aware reward (M2) is a
> separate, lighter path described in `docs/ROADMAP.md`.

---

## 0. Prerequisites & hardware guardrails

| Item | Requirement | Your env |
|---|---|---|
| OS | Ubuntu 22.04 LTS (20.04 ok, 24.04 not yet UE-validated) | ✅ assumed |
| GPU | NVIDIA, proprietary driver ≥ 535, CUDA 12.x | 8-16GB |
| Disk | **~150 GB free** on the UE build volume (source ~40GB, DerivedDataCache + builds ~100GB) | verify first |
| RAM | ≥ 32 GB recommended (linker peak) | verify |
| Network | GitHub + Epic Games access; GitHub account linked to Epic for UE source | required |

**8-16GB VRAM constraints** (important — defaults will OOM):

- Disable hardware **Lumen** GI; use baked/stationary lighting or Skylight only.
- Disable **Nanite** on imported meshes (it also fights OBJ import).
- Render targets: start at **512x512**, 6 views. 1024x1024 only if VRAM headroom confirmed.
- `r.Shadow.MaxResolution 512`, disable ray tracing entirely.
- `r.TextureStreaming=1`, pool size ≥ 2048.

---

## 1. System packages

Run once on a clean Ubuntu install. Xvfb is mandatory because UE on a headless
box needs a virtual X display even in `-RenderOffScreen` mode for some GPU paths.

```bash
sudo apt update
sudo apt install -y \
  build-essential clang git git-lfs curl wget unzip p7zip-full \
  xorg xvfb libxrandr-dev libx11-dev libxcursor-dev libxinerama-dev \
  libxi-dev libgl1-mesa-dev libglu1-mesa-dev libasound2-dev libfreetype6-dev \
  libfontconfig1-dev libssl-dev libudev-dev libpulse-dev \
  nvidia-driver-535 nvidia-utils-535
```

Verify the GPU is visible:

```bash
nvidia-smi
# Expect: Driver Version: 535.x, CUDA Version: 12.x, your GPU listed.
```

If `nvidia-smi` fails or shows no GPU, stop here — UE headless GPU rendering
will not work. Fix the driver first.

---

## 2. Clone and build UE5 from source

You need an Epic Games account with GitHub linked (see
https://github.com/EpicGames/UnrealEngine — accept the invite from your Epic
account profile). Use a release tag for stability, not `master`.

```bash
# Use a dedicated volume with space; ~40GB source, ~100GB builds/DDC.
mkdir -p ~/ue && cd ~/ue
git clone -b 5.4 --depth 1 https://github.com/EpicGames/UnrealEngine.git UE5
cd UE5

# Required: this configures git LFS and sets up the dependency bundle.
./Setup.sh        # downloads ~20GB of binary deps; slow
./GenerateProjectFiles.sh
```

### Headless build (no editor GUI, smaller, faster to iterate)

On a remote server you want the editor built as a service host. Build it in
**`Development`**, not `Shipping` — Shipping drops automation/development
support and is useless for the service host. The generated Makefile's *default*
target is not reliable across environments (on some setups it builds
`Shipping`), so call the Unreal Build Tool directly to pin the configuration.
`Build.sh` is what the Makefile calls under the hood, and its target/config
arguments are stable:

```bash
# From the UE5 root (after Setup.sh + GenerateProjectFiles.sh):
./Engine/Build/BatchFiles/Linux/Build.sh \
    UnrealEditor Linux Development -WaitMutex
# ~20-40 min on a decent CPU.
# Output binary: Engine/Binaries/Linux/UnrealEditor
```

You also need the shader compiler worker — the editor spawns it on every startup
to compile materials/shaders. If it's missing, the `-version` sanity command
below fails with `ShaderCompileWorker not found`. Build it with the same `Build.sh`,
same config (Development):

```bash
./Engine/Build/BatchFiles/Linux/Build.sh \
    ShaderCompileWorker Linux Development -WaitMutex
# ~5-10 min. Output: Engine/Binaries/Linux/ShaderCompileWorker
```

Positional args are: `<Target> <Platform> <Configuration>`. Do **not** pass
`TargetPlatform=`, `Development Editor` (the space breaks Make), or an invented
target like `UE5Editor` — all of these trigger "No rule to make target".
`UnrealEditor` and `ShaderCompileWorker` are real targets; build both. If you
prefer the Makefile, force Development with an explicit target arg it actually
defines, e.g. `make Development` — but `Build.sh` is the most portable form.

Verify the binary runs headless. `UnrealEditor` is a full editor process: `-version`
only prints the version to the log early in init — it does **not** make the process
exit, so the command will appear to hang in the event loop. Wrap it in `timeout` so
the sanity check always returns, and treat "got the version line + clean exit via
timeout" as success:

```bash
timeout 60 ./Engine/Binaries/Linux/UnrealEditor -version -RenderOffScreen -NoLoadStartupPackages 2>&1 | tee /tmp/ue_version.log
# Expect: a version line near the top, then timeout kills it at 60s (exit 124).
# If it dies immediately with a crash/error, fix that before proceeding.
grep -i "engine version\|UnrealEngine" /tmp/ue_version.log | head
```

A truly self-terminating check is to run a real commandlet instead (`-run=` targets
exit on completion), but none ships with the engine that needs no project — so the
`timeout` wrapper above is the pragmatic smoke test at this stage.

> If you still get `ShaderCompileWorker` errors after building it, check that
> `Engine/Binaries/Linux/ShaderCompileWorker` exists and is executable, and that
> you didn't mix configs — worker and editor must both be `Development`.

Set an alias for sanity (put in `~/.bashrc`):

```bash
export UE_EDITOR="$HOME/ue/UE5/Engine/Binaries/Linux/UnrealEditor"
```

---

## 3. The render service project (`ue/AutoMeshRender/`)

The service is already implemented in this repo under `ue/AutoMeshRender/`.
It is a persistent UE5 module (not a per-call commandlet) that:

1. Starts an HTTP listener on `127.0.0.1:<port>` from `StartupModule` (via
   `FCoreDelegates::OnEndFrame`, once the game viewport exists).
2. On `POST /reward`, parses `{"original_obj","current_obj","step","faces"}`,
   renders each mesh from 6 fixed cube-face cameras into a 512² depth RT,
   and returns `{"reward": 1/(1+MSE_depth)}`.
3. On `POST /render`, parses `{"obj","width","height"}`, renders the single
   mesh from one auto-framed 3/4 camera into a 1024² colour RT, and returns
   the PNG bytes (`Content-Type: image/png`).

### Project layout

```
ue/AutoMeshRender/
├── AutoMeshRender.uproject              module decl (HTTPServer is an engine module, not a plugin)
├── Source/
│   ├── AutoMeshRenderEditor.Target.cs   Editor target — BUILD THIS (Type=Editor)
│   ├── AutoMeshRender.Target.cs         Game target ��� cooked-only, not used in dev
│   └── AutoMeshRender/
│       ├── AutoMeshRender.Build.cs      deps: HTTPServer, Json, ProceduralMeshComponent, ...
│       ├── AutoMeshRender.cpp           IMPLEMENT_PRIMARY_GAME_MODULE + OnEndFrame hook
│       ├── RenderService.cpp            HTTP listener (IHttpRouter) + /reward and /render routes
│       └── MeshComparator.cpp           OBJ parse → ProceduralMesh; /reward: 6-view depth→MSE; /render: 3/4 view→PNG
├── Config/
│   ├── DefaultEngine.ini                VRAM caps (512 RT, no Lumen/Nanite/RT)
│   └── DefaultGame.ini                  bShareMaterialShaderCode=False (shader lib off)
├── build.sh                             build helper (takes UE_ROOT, --target, --config)
└── README.md                            full build/run/verify + verified API notes
```

Build and run instructions are in `docs/AUTOMESH_RENDER_BUILD.md` (§2 build,
§5 run). The README's "API notes" section lists every UE5.4 API the code uses,
all verified against the actual engine headers on the build host.

---

## 4. Build and run the AutoMeshRender service

The render service lives in `ue/AutoMeshRender/` (in the automesh repo). It is a
**persistent HTTP server** run via the engine editor in `-game` mode — NOT a
per-call subprocess. Build and run instructions are in
`docs/AUTOMESH_RENDER_BUILD.md` and `ue/AutoMeshRender/README.md`; the short
version:

```bash
# Build the Editor target (produces libUnrealEditor-AutoMeshRender.so):
./ue/AutoMeshRender/build.sh "$UE_ROOT" --target AutoMeshRenderEditor

# Run the persistent service (editor -game, uncooked, headless):
xvfb-run -a -s "-screen 0 1280x720x24" \
  "$UE_ROOT/Engine/Binaries/Linux/UnrealEditor" \
  "$PROJ" \
  -game -RenderOffScreen -NoLoadStartupPackages -Unattended -NoSplash -NoPause \
  -windowed -resx=1 -resy=1 -RenderServicePort=8765 -log
# wait for: [AutoMeshRender] HTTP service listening on 127.0.0.1:8765/reward and /render
```

Key point: use the **editor** (`UnrealEditor -game`), not the Game-target
binary. The editor loads uncooked content and compiles shaders online; the
Game binary runs in cooked mode and fatal-exits without a full packaging pass.

### Wire it into the training loop

From Python, call the warm service over HTTP via `HttpRenderReward` (not
`ExternalCommandReward` — that cold-starts a subprocess per call):

```python
from automesh.rewards import HttpRenderReward

ue_reward = HttpRenderReward(
    endpoint="http://127.0.0.1:8765/reward",
    interval=100,          # sparse: only every 100 steps
    weight=1.0,
    timeout_seconds=30.0,  # warm call is tens of ms; 30s is a safety net
)
```

`HttpRenderReward` serializes both meshes to OBJ text with `mesh_to_obj_string`,
POSTs them in the body, and scales the returned float by `weight`. No shared
volume is required — the meshes travel in the request. Because the service is
warm, `timeout_seconds` drops from 300s (subprocess) to 30s.

To render a single mesh to a PNG (for inspection / paper figures), use the
`/render` route — either `render_mesh_image(mesh, endpoint=...)` from Python or
the CLI:

```bash
automesh render-image input.obj out.png --endpoint http://127.0.0.1:8765/render
# out.png is a 1024x1024 shaded 3/4 view of the mesh
```

---

## 5. First-run sanity checks (do these before training)

```bash
# 1. Service is up and answering. Mesh content travels in the body.
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"original_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","current_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","step":0,"faces":1}))')"
# Expect: {"reward": <float>}  (1.0 for identical meshes)

# 2. /render returns a PNG of a single 3/4 view (default 1024x1024).
curl -s -X POST http://127.0.0.1:8765/render \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","width":1024,"height":1024}))')" \
  -o /tmp/mesh.png
file /tmp/mesh.png   # Expect: PNG image data, 1024 x 1024

# 3. End-to-end from Python with the real class.
python - <<'PY'
from automesh.rewards import HttpRenderReward
from automesh.mesh import Mesh
r = HttpRenderReward(endpoint="http://127.0.0.1:8765/reward", interval=1)
# build a RewardContext and call r(ctx) to confirm the full path
print("client ready")
PY
```

---

## 6. Shader warm-up (persistent service)

Because the service runs as a **persistent** editor process (not a per-call
subprocess), shaders compile on the first few reward calls and then stay warm
in the DDC — there is no per-call cold start and no 300s timeout to blow. The
first `curl /reward` after startup may take a few minutes while the engine
compiles the depth-capture shaders for the GPU; subsequent calls reuse them.

To pre-warm before training, just send one throwaway request after startup and
let it finish:

```bash
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"original_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","current_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","step":0,"faces":1}))')"
# first call slow (shader compile), later calls tens of ms
```

Point the DDC at a shared on-disk path in `DefaultEngine.ini` so compiles
persist across service restarts:

```ini
[DerivedDataCache]
StaticPaths="/path/to/ddc"
```

---

## 7. VRAM budget cheatsheet (8-16GB)

| Resource | Default | Headless reward | Notes |
|---|---|---|---|
| Render target | 1080p | **512x512** | 6 views = 6 MB backbuffer, trivial |
| Shadow maps | 2k | **512** | `r.Shadow.MaxResolution=512` |
| Texture pool | 1GB | **2GB** | `r.Streaming.PoolSize=2048` |
| Lumen | on | **off** | Eats 4-6GB alone |
| Nanite | on | **off** | Incompatible with OBJ import anyway |
| Ray tracing | off | off | Confirmed off |

If you still OOM: drop to 256x256, 4 views, and ensure no other GPU process
(`nvidia-smi`) is competing.

---

## 8. Iteration tips

- **Keep OBJ import minimal**: `ProceduralMeshComponent`, no material slots, no
  UVs. The reward only cares about silhouette/depth/normal, not textures.
- **stdout discipline**: UE_LOG goes to the editor log, not stdout. Only the
  HTTP response body carries the reward, so `HttpRenderReward` parsing stays
  clean — do not print the float to stdout.
- **Cache camera matrices** in the service at startup; don't recompute per call.
- **Mesh transport**: OBJ text travels in the request body (`original_obj` /
  `current_obj`), so client and service need no shared volume. This means the
  service can run on a different host than training with no NFS setup; just
  point `endpoint` at the remote box. Keep meshes modest in size — a very dense
  mesh makes for a large JSON payload, and `interval=100` keeps that rare.
- **Failure isolation**: a bad mesh that crashes the comparator should be caught
  inside the service and returned as HTTP 500, not propagated to kill training.
  `HttpRenderReward` raises on non-2xx, so prefer catch-and-500 inside the
  service for known-bad inputs and reserve hard crashes for truly unexpected
  state.
- **Use `--no-weld` on the automesh sampling script** when feeding meshes to UE:
  UE's OBJ importer does its own welding; double-welding wastes time.

---

## 9. What this unblocks in the roadmap

- **M4 (Sparse Photoreal Reward)**: `HttpRenderReward` now has a real backend.
  Replace `PhotorealRewardStub` with the UE service wired as in section 4.
- **M5 (Evaluation)**: the `POST /render` route is already live — it takes
  `{"obj": <obj text>, "width": 1024, "height": 1024}` and returns a PNG of a
  single auto-framed 3/4 view. Drive it from `automesh render-image in.obj
  out.png --endpoint http://127.0.0.1:8765/render` to export paper figures,
  reusing the warm renderer (no separate cook/packaging step).
