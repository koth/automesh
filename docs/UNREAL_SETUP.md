# Unreal Engine Reward Setup (Ubuntu, headless, NVIDIA 8-16GB)

Goal: build a **headless UE5 commandlet** that the existing `ExternalCommandReward`
in `src/automesh/rewards.py` can call as a sparse photoreal reward (Roadmap M4).

The contract from `ExternalCommandReward.__call__` is fixed and drives every
decision below:

- Input: two OBJ paths passed via a templated command line:
  `{original}`, `{current}`, `{step}`, `{faces}`.
- Output: the **last line of stdout must be a single float** (similarity reward).
- Failure mode: any non-zero exit or timeout (>300s default) raises and aborts
  the training step, so the commandlet must be robust and self-contained.
- Only mesh paths are exchanged. Cameras, materials, lighting, and the loss
  are all owned by the UE side.

> Scope note: this document covers the Unreal side. nvdiffrast/PyTorch3D for
> the dense render-aware reward (M2) is a separate, lighter path described in
> `docs/ROADMAP.md`.

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

On a remote server you want the editor built as a commandlet host. Build it in
**`Development`**, not `Shipping` — Shipping drops commandlet/automation support
and is useless for the service host. The generated Makefile's *default* target is
not reliable across environments (on some setups it builds `Shipping`), so call
the Unreal Build Tool directly to pin the configuration. `Build.sh` is what the
Makefile calls under the hood, and its target/config arguments are stable:

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

## 3. Create the reward project + OBJ-import commandlet

This is the bridge between `ExternalCommandReward` and UE. Create a blank C++
project, then add a commandlet that:

1. Parses `-original=<path>` `-current=<path>` from the command line.
2. Imports both OBJ files into ProceduralMeshComponents.
3. Renders each from N fixed cameras to render targets.
4. Reads back pixels, computes a similarity metric (start with MSE / SSIM, upgrade to LPIPS later).
5. Prints a single float to stdout and exits 0.

### Scaffold the project

From the UE editor build (still headless, via commandlet):

```bash
$UE_EDITOR /path/to/AutoMeshReward/AutoMeshReward.uproject \
  -run=GenerateProjectFiles -RenderOffScreen
```

Or create the `.uproject` + `Source/` tree by hand following the
`AutoMeshReward` layout below.

### Project layout

```
AutoMeshReward/
├── AutoMeshReward.uproject
├── Source/
│   └── AutoMeshReward/
│       ├── AutoMeshReward.Build.cs
│       ├── AutoMeshReward.cpp            # module impl
│       └── RewardCommandlet.cpp          # the bridge
└── Config/
    └── DefaultEngine.ini                 # disable Lumen/Nanite/RT
```

### RewardCommandlet.cpp (minimal skeleton)

```cpp
// Reads two OBJs, renders both from fixed cameras, prints similarity float.
// This is intentionally minimal — flesh out the import + render loops.

#include "Commandlets/Commandlet.h"
#include "Misc/CommandLine.h"
#include "Misc/Paths.h"

DECLARE_LOG_CATEGORY_CLASS(LogRewardCmdlet, Log, All);

class URewardCommandlet : public UCommandlet
{
    GENERATED_BODY()
public:
    URewardCommandlet() { LogToConsole = true; IsClient = false; IsEditor = true; }

    virtual int32 Main(const FString& Params) override
    {
        // Parse: -original=<abs path> -current=<abs path> -out=<csv path>
        FString OriginalPath = FParse::Token(*Params, false);
        // ...strip "-original=" prefix, repeat for current, out

        UE_LOG(LogRewardCmdlet, Display, TEXT("original=%s current=%s"), *OriginalPath, *CurrentPath);

        // 1. Import OBJs into ProceduralMeshComponents.
        // 2. Set up 6 fixed cameras (cube-face or fixed turntable).
        // 3. Render each to a UTextureRenderTarget2D (512x512).
        // 4. Read back FColor arrays via ReadSurfaceData.
        // 5. Compute similarity = 1 / (1 + mean(|orig - curr|^2)).
        float Similarity = 0.0f; // <-- replace with real metric

        // CRITICAL: the automesh side reads the LAST line of stdout as a float.
        fprintf(stdout, "%f\n", Similarity);
        fflush(stdout);
        return 0;  // non-zero exit aborts the training step
    }
};
```

### DefaultEngine.ini (VRAM + headless guards)

```ini
[SystemSettings]
r.Shadow.MaxResolution=512
r.TextureStreaming=True
r.Streaming.PoolSize=2048
r.Lumen.ScreenProbeGather.TracingOctahedronResolution=8
r.Nanite.Enabled=False
r.RayTracing=False
r.AntiAliasingMethod=0
r.RenderTargetViewportSize=512
```

---

## 4. Invocation via ExternalCommandReward

Wire it up from Python. The template placeholders `{original}` `{current}`
`{step}` `{faces}` are filled in by `ExternalCommandReward` automatically.

```python
from automesh.rewards import ExternalCommandReward

ue_reward = ExternalCommandReward(
    command=[
        "xvfb-run", "-a", "-s", "-screen 0 1280x720x24",
        "$UE_EDITOR",
        "/abs/path/AutoMeshReward/AutoMeshReward.uproject",
        "-run=RewardCommandlet",
        "-original={original}",
        "-current={current}",
        "-step={step}",
        "-faces={faces}",
        "-RenderOffScreen",
        "-NoLoadStartupPackages",
        "-NoShaderCompile",
        "-Unattended",
        "-NoPause",
        "-NoSplash",
        "-ExitUponCompletion",
    ],
    interval=100,        # sparse: only every 100 steps
    weight=1.0,
    timeout_seconds=300, # matches default; bump if first-run shader compile is slow
)
```

### Why each flag

| Flag | Reason |
|---|---|
| `xvfb-run -a` | Headless server has no X; UE needs a display for some GPU init paths. |
| `-run=RewardCommandlet` | Invokes our bridge; replaces the editor event loop. |
| `-RenderOffScreen` | No window/SwapChain; required for SSH. |
| `-NoLoadStartupPackages` | Faster cold start; our commandlet loads only what it needs. |
| `-NoShaderCompile` | Avoids 5-10 min first-run stall. **Pre-compile shaders first** (section 6). |
| `-Unattended` `-NoPause` `-NoSplash` | No dialogs/prompts that would hang a subprocess. |
| `-ExitUponCompletion` | Ensures the process terminates so `subprocess.run` returns. |

---

## 5. First-run sanity checks (do these before training)

Each check should be run standalone and produce the expected output before
chaining them.

```bash
# 1. Editor launches headless and exits cleanly.
xvfb-run -a -s "-screen 0 1280x720x24" \
  $UE_EDITOR /abs/path/AutoMeshReward/AutoMeshReward.uproject \
  -RenderOffScreen -NoLoadStartupPackages -ExitUponCompletion

# 2. Commandlet runs and prints a float on stdout.
xvfb-run -a -s "-screen 0 1280x720x24" \
  $UE_EDITOR /abs/path/AutoMeshReward/AutoMeshReward.uproject \
  -run=RewardCommandlet \
  -original=/abs/original.obj -current=/abs/current.obj \
  -RenderOffScreen -Unattended -ExitUponCompletion
# Expect last stdout line: 0.000000 (or whatever the metric returns)

# 3. Python bridge end-to-end with a tiny synthetic reward command.
python -c "
from automesh.rewards import ExternalCommandReward
from automesh.mesh import Mesh
from automesh.qem import CollapseCandidate
r = ExternalCommandReward(command=['echo', '0.42'], interval=1, weight=1.0)
# Minimal context (construct directly for the smoke test)
print('ok' if r.__call__ else 'fail')
"
```

---

## 6. Shader precompile (avoids first-call OOM/timeout)

The first time the commandlet renders, UE compiles shaders for the GPU. On a
cold cache this takes 5-10 minutes and can blow the 300s `timeout_seconds`.
Pre-populate the DerivedDataCache once:

```bash
xvfb-run -a -s "-screen 0 1280x720x24" \
  $UE_EDITOR /abs/path/AutoMeshReward/AutoMeshReward.uproject \
  -run=ShaderCompile -RenderOffScreen -NoLoadStartupPackages -ExitUponCompletion
```

After this, `-NoShaderCompile` in the reward invocation is safe. Point the DDC
at a shared on-disk path in `DefaultEngine.ini`:

```ini
[Core.Log]
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

- **Keep OBJ import minimal**: ProceduralMeshComponent, no material slots, no UVs. The reward only cares about silhouette/depth/normal, not textures.
- **Log to stderr, float to stdout**: UE_LOG goes to the editor log, not stdout. Use `fprintf(stdout, ...)` + `fflush` for the reward value only. Anything else will confuse `ExternalCommandReward`'s "last line = float" parse.
- **Cache camera matrices** in the commandlet; don't recompute per call.
- **One process per call is expensive**: 10-20s UE startup per reward call is normal. That's why `interval=100` is the default — sparse by design. If startup dominates, consider a persistent UE process + a socket/pipe bridge (a future enhancement, not needed for M4 validation).
- **Use `--no-weld` on the automesh sampling script** when feeding meshes to UE: UE's OBJ importer does its own welding; double-welding wastes time.

---

## 9. What this unblocks in the roadmap

- **M4 (Sparse Photoreal Reward)**: `ExternalCommandReward` now has a real backend. Replace `PhotorealRewardStub` usage with the UE commandlet wired as in section 4.
- **M5 (Evaluation)**: the same commandlet, invoked directly (not via the reward hook), can render final simplified meshes for paper figures. Add a `--save-images` mode to the commandlet for batch evaluation runs.
# Unreal Engine Reward Setup (Ubuntu, headless, NVIDIA 8-16GB)

Goal: run a **persistent headless UE5 render service** on the training box,
and call it from `HttpRenderReward` in `src/automesh/rewards.py` over HTTP as
the sparse photoreal reward (Roadmap M4).

## Architecture

```
  automesh training loop (Python)
       │  every N steps (interval=100)
       ▼
  HttpRenderReward  (urllib POST, stdlib only)
       │  body: {"original": "/shared/orig_100.obj", "current": "/shared/curr_100.obj",
       │         "step": 100, "faces": 456}
       ▼  127.0.0.1 HTTP  (single call, tens of ms)
  UE5 render service  (persistent process, GPU stays warm)
       │  load OBJ → 6 fixed cameras → render targets → similarity metric
       ▼
  {"reward": 0.83}
```

### Why not `ExternalCommandReward` (subprocess per call)

`ExternalCommandReward` does `subprocess.run` on every call. Applied to UE that
means a cold process + shader/GPU init + Xvfb spin-up **per reward call**
(10-20s each), and it contradicts "the renderer is a service". `HttpRenderReward`
instead assumes a long-running renderer; each reward is one HTTP request, so GPU
state, shaders, and camera setup are reused and the 300s cold-start timeout trap
disappears entirely.

The contract between client and service:

- **Request**: `POST <endpoint>`, `Content-Type: application/json`,
  body `{"original": <abs path>, "current": <abs path>, "step": int, "faces": int}`.
  Meshes are exchanged **by path on a shared filesystem** (same box), never
  serialized over the wire.
- **Response**: `200` with `{"reward": <float>}`. A non-2xx or malformed body
  raises and aborts the training step (strict failure, same as the subprocess path).
- **Mesh scope**: only mesh paths are exchanged. Cameras, materials, lighting,
  and the loss live entirely in the UE service.

> Scope note: nvdiffrast/PyTorch3D for the dense render-aware reward (M2) is a
> separate, lighter path described in `docs/ROADMAP.md`.

---

## 0. Prerequisites & hardware guardrails

| Item | Requirement | Your env |
|---|---|---|
| OS | Ubuntu 22.04 LTS (20.04 ok, 24.04 not yet UE-validated) | ✅ assumed |
| GPU | NVIDIA, proprietary driver ≥ 535, CUDA 12.x | 8-16GB |
| Disk | **~150 GB free** (UE source ~40GB, builds + DDC ~100GB) | verify first |
| RAM | ≥ 32 GB recommended (linker peak) | verify |
| Network | GitHub + Epic Games; GitHub account linked to Epic for UE source | required |

**8-16GB VRAM constraints** (defaults will OOM):

- Disable hardware **Lumen** GI; use baked/stationary lighting or Skylight only.
- Disable **Nanite** on imported meshes (also fights OBJ import).
- Render targets: start at **512x512, 6 views**. 1024^2 only with VRAM headroom.
- `r.Shadow.MaxResolution 512`, ray tracing off.
- `r.TextureStreaming=1`, pool size ≥ 2048.

---

## 1. System packages

Xvfb is still needed: the persistent UE service runs `-RenderOffScreen`, but some
GPU init paths still want a display present.

```bash
sudo apt update
sudo apt install -y \
  build-essential clang git git-lfs curl wget unzip p7zip-full \
  xorg xvfb libxrandr-dev libx11-dev libxcursor-dev libxinerama-dev \
  libxi-dev libgl1-mesa-dev libglu1-mesa-dev libasound2-dev libfreetype6-dev \
  libfontconfig1-dev libssl-dev libudev-dev libpulse-dev \
  nvidia-driver-535 nvidia-utils-535
nvidia-smi   # must show the GPU + driver 535.x / CUDA 12.x
```

---

## 2. Clone and build UE5 from source

Needs an Epic account with GitHub linked (https://github.com/EpicGames/UnrealEngine).
Use a release tag, not `master`.

```bash
mkdir -p ~/ue && cd ~/ue
git clone -b 5.4 --depth 1 https://github.com/EpicGames/UnrealEngine.git UE5
cd UE5
./Setup.sh                 # ~20GB binary deps
./GenerateProjectFiles.sh
./Engine/Build/BatchFiles/Linux/Build.sh \
    UnrealEditor Linux Development -WaitMutex   # 20-40 min
export UE_EDITOR="$HOME/ue/UE5/Engine/Binaries/Linux/UnrealEditor"

# sanity: launches headless. -version does NOT self-exit (full editor event loop),
# so wrap in timeout; success = version line printed + clean 60s timeout kill.
timeout 60 $UE_EDITOR -version -RenderOffScreen -NoLoadStartupPackages 2>&1 | tee /tmp/ue_version.log
grep -i "engine version\|UnrealEngine" /tmp/ue_version.log | head
```

Build **`Development`** explicitly, **not `Shipping`** — Shipping drops
commandlet/automation support that the service host relies on. The generated
Makefile's default target is environment-dependent (it may be `Shipping`), so
pin the config via `Build.sh`'s positional `<Target> <Platform> <Configuration>`
args as shown above. Do not pass `TargetPlatform=`, `Development Editor`, or
invented targets like `UE5Editor` — they all trigger "No rule to make target".

---

## 3. Build the render service project

Create a blank C++ project with two pieces:

1. A **mesh import + render + metric** core (same logic as the old commandlet).
2. An **HTTP listener** that accepts `POST /reward`, runs the core, returns
   `{"reward": <float>}`.

### Project layout

```
AutoMeshRender/
├── AutoMeshRender.uproject
├── Source/AutoMeshRender/
│   ├── AutoMeshRender.Build.cs
│   ├── AutoMeshRender.cpp
│   ├── RenderService.cpp        # HTTP listener + request loop
│   └── MeshComparator.cpp       # OBJ import → 6 cameras → similarity
└── Config/DefaultEngine.ini
```

### RenderService.cpp (minimal listener skeleton)

UE has no built-in HTTP *server*, so embed a tiny one. The example below uses
the shipped `HttpServerModule` (available since UE 5.0):

```cpp
#include "HttpServerModule.h"
#include "Interfaces/IHttpServer.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

void StartRenderService(int32 Port)
{
    IHttpServer& Server = FHttpServerModule::Get().GetServer();

    Server.AddRoute(TEXT("/reward"), EHttpServerRequestVerbs::VERB_POST,
        [](const FHttpServerRequest& Req, const FHttpResultCallback& OnComplete) {
            // Parse JSON body: { original, current, step, faces }
            TSharedPtr<FJsonObject> Body;
            TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Req.Body);
            FJsonSerializer::Deserialize(Reader, Body);

            const FString OriginalPath = Body->GetStringField(TEXT("original"));
            const FString CurrentPath  = Body->GetStringField(TEXT("current"));

            float Reward = UMeshComparator::ComputeSimilarity(OriginalPath, CurrentPath);

            TSharedPtr<FJsonObject> RespJson = MakeShareable(new FJsonObject);
            RespJson->SetNumberField(TEXT("reward"), Reward);
            FString RespStr;
            TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&RespStr);
            FJsonSerializer::Serialize(RespJson.ToSharedRef(), Writer);

            FHttpServerResponse Resp;
            Resp.Code = EHttpServerResponseCodes::Ok;
            Resp.Body = TCHAR_TO_UTF8(*RespStr);
            Resp.Headers.Add(TEXT("Content-Type"), {TEXT("application/json")});
            OnComplete(MakeUnique<FHttpServerResponse>(MoveTemp(Resp)));
            return true;
        });

    Server.Start(Port);
}
```

`UMeshComparator::ComputeSimilarity` is the meat: import both OBJs into
`ProceduralMeshComponent`s, render each from 6 fixed cameras to 512x512
`UTextureRenderTarget2D`, read pixels back, compute similarity (start MSE/SSIM,
upgrade to LPIPS later), return a float. See section 6 for the VRAM settings it
must respect.

### DefaultEngine.ini (VRAM + headless guards)

```ini
[SystemSettings]
r.Shadow.MaxResolution=512
r.TextureStreaming=True
r.Streaming.PoolSize=2048
r.Lumen.ScreenProbeGather.TracingOctahedronResolution=8
r.Nanite.Enabled=False
r.RayTracing=False
r.AntiAliasingMethod=0
r.RenderTargetViewportSize=512
```

---

## 4. Run the service + call it from automesh

### Start the persistent service (once per training run)

```bash
# Shared volume: automesh writes OBJs here, UE reads them. Same box, no network copy.
mkdir -p /shared/automesh_reward

xvfb-run -a -s "-screen 0 1280x720x24" \
  $UE_EDITOR /abs/AutoMeshRender/AutoMeshRender.uproject \
  -game -RenderOffScreen -NoLoadStartupPackages -Unattended \
  -NoSplash -NoPause -windowed -resx=1 -resy=1 \
  -RenderServicePort=8765
# listens on 127.0.0.1:8765, stays up for the whole run
```

### Wire it into the training loop

```python
from automesh.rewards import HttpRenderReward

ue_reward = HttpRenderReward(
    endpoint="http://127.0.0.1:8765/reward",
    interval=100,          # sparse: only every 100 steps
    weight=1.0,
    timeout_seconds=30.0,  # warm call should be tens of ms; 30s is a safety net
    shared_dir="/shared/automesh_reward",  # volume both processes can read
)
```

`HttpRenderReward` writes `orig_<step>.obj` / `curr_<step>.obj` into
`shared_dir`, POSTs the paths, and scales the returned float by `weight`.
Because the service is warm, `timeout_seconds` can drop from 300s (subprocess)
to 30s.

---

## 5. Sanity checks (before training)

```bash
# 1. Service is up and answering.
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d '{"original":"/shared/automesh_reward/orig_0.obj",
       "current":"/shared/automesh_reward/curr_0.obj","step":0,"faces":0}'
# Expect: {"reward": <float>}

# 2. End-to-end from Python with the real class.
python - <<'PY'
from automesh.rewards import HttpRenderReward
from automesh.mesh import Mesh
r = HttpRenderReward(endpoint="http://127.0.0.1:8765/reward",
                     interval=1, shared_dir="/shared/automesh_reward")
# build a RewardContext and call r(ctx) to confirm the full path
print("client ready")
PY
```

---

## 6. VRAM budget (8-16GB)

| Resource | Default | Reward service | Notes |
|---|---|---|---|
| Render target | 1080p | **512x512** | 6 views = trivial |
| Shadow maps | 2k | **512** | `r.Shadow.MaxResolution=512` |
| Texture pool | 1GB | **2GB** | `r.Streaming.PoolSize=2048` |
| Lumen | on | **off** | 4-6GB alone |
| Nanite | on | **off** | Incompatible with OBJ import |
| Ray tracing | off | off | confirmed off |

Because the service is persistent, the DDC/shader cache warms up once during
the first few reward calls and then stays warm — there is no per-call
cold-start. If the very first call is slow, let it finish once; subsequent
calls reuse compiled shaders.

---

## 7. Iteration tips

- **stdout discipline**: UE_LOG goes to the editor log, not stdout. Only the
  HTTP response body carries the reward, so `HttpRenderReward` parsing is clean.
- **OBJ import**: `ProceduralMeshComponent`, no material slots, no UVs. The
  reward cares about silhouette/depth/normal, not textures.
- **Camera matrices**: cache them in the service at startup; don't recompute
  per call.
- **Shared volume is the contract**: both processes must see the same
  `/shared/automesh_reward`. On a single box this is just a local dir; if you
  later go cross-box, switch to NFS or serialize the mesh into the body.
- **Failure isolation**: a bad mesh that crashes the comparator should be
  caught inside the service and returned as HTTP 500, not propagated to kill
  the training process. `HttpRenderReward` will raise on non-2xx, so prefer to
  catch-and-500 inside the service for known-bad inputs and reserve hard
  crashes for truly unexpected state.

---

## 8. What this unblocks in the roadmap

- **M4 (Sparse Photoreal Reward)**: `HttpRenderReward` now has a real backend.
  Replace `PhotorealRewardStub` with the UE service wired as in section 4.
- **M5 (Evaluation)**: add a `GET /render?mesh=<path>&view=<n>` route to the
  same service to export paper figures, reusing the warm renderer.
