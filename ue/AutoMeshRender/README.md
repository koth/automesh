# AutoMeshRender — persistent headless UE5 render service

Part of the automesh M4 sparse photoreal reward. Implements the server side of
the `HttpRenderReward` contract in `src/automesh/rewards.py`:

```
POST /reward  {"original": <abs path>, "current": <abs path>, "step": int, "faces": int}
-> 200 {"reward": <float>}
```

## Build

Requires UE5 built from source (see `docs/UNREAL_SETUP.md`), with
`UnrealEditor` + `ShaderCompileWorker` already in `Development`.

Build this project the same way you built the engine: via UBT's `Build.sh`,
with the project passed via `-Project=`. The target name `AutoMeshRender`
matches `AutoMeshRender.Target.cs`. **Generate project files is implicit** —
UBT does it as part of the build, so you do not run a separate `-projectfiles`
step.

```bash
# From the UE5 root (or anywhere UE_EDITOR is set):
export UE_ROOT="$HOME/ue/UE5"
export UBT="$UE_ROOT/Engine/Build/BatchFiles/Linux/Build.sh"
export PROJ="/abs/automesh/ue/AutoMeshRender/AutoMeshRender.uproject"

# 1) Generate project files + build the AutoMeshRender target (Development, Linux):
"$UBT" AutoMeshRender Linux Development -Project="$PROJ" -WaitMutex
# Output: ue/AutoMeshRender/Binaries/Linux/AutoMeshRender
```

Notes:
- `-Project=` is required so UBT uses this `.uproject` instead of the engine's.
- If UBT complains the target doesn't exist, check that
  `Source/AutoMeshRender.Target.cs` is present (class name
  `AutoMeshRenderTarget`, file name `AutoMeshRender.Target.cs`). It must live
  in `Source/`, not inside the module dir — see `docs/AUTOMESH_RENDER_BUILD.md` §4a.
- If it asks for a `Client`/`Server` target, you only have a Game target; that's
  intentional — the service runs with `-game`.

## Run

```bash
xvfb-run -a -s "-screen 0 1280x720x24" \
  $UE_EDITOR "/abs/automesh/ue/AutoMeshRender/AutoMeshRender.uproject" \
  -game -RenderOffScreen -NoLoadStartupPackages -Unattended -NoSplash -NoPause \
  -windowed -resx=1 -resy=1 -RenderServicePort=8765
```

The module's `StartupModule` hooks `OnEndFrame` and starts the HTTP listener
once the game viewport exists, so no map asset is required.

## Verify

```bash
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d '{"original":"/shared/o.obj","current":"/shared/c.obj","step":0,"faces":0}'
# -> {"reward": 0.0...}
```

## Known compile-risk points (verify on your UE5.4)

This code was written against public UE5.4 APIs but not compiled in this repo
(no UE toolchain here). The following are the spots most likely to need a tweak
on your machine:

1. **`MeshComparator.cpp` — `ReadSurfaceData`**. Must run on the render thread
   with an `FRHICommandList`. Current code uses `ENQUEUE_RENDER_COMMAND` +
   `RHICmdList.ReadSurfaceData`. If your build prefers the resource's own method,
   use `RtRes->ReadSurfaceFloatData(...)`. The captured `&Pixels` is safe only
   because `Fence.Wait()` blocks below; do not remove the fence.

2. **`MeshComparator.cpp` — `UTextureRenderTarget2D::Init`**. `RTF_RGBA8` + `Init`
   is the common 5.4 path. If you only get depth via `RTF_R32f`, switch format
   and adjust the `CompareBuffers` channel (currently reads `Va[p].R`).

3. **`MeshComparator.cpp` — `USceneCaptureComponent2D`**. `CaptureSource =
   SCS_SceneDepth` + `ShowFlags.SetDepthOnlyTest(true)` is the stable form. If
   you want silhouette instead of depth, switch to `SCS_SceneColor` and a
   depth-only material, or render the actor with a flat white material and read
   `SCS_FinalColorLDR` alpha.

4. **`RenderService.cpp` — `FHttpRouteHandler` / `EHttpServerRequestVerbs`**.
   These live in the `HTTPServer` engine module (a build dependency in
   `AutoMeshRender.Build.cs`, NOT a `.uproject` plugin — it ships with the engine,
   at `Engine/Source/Runtime/Online/HTTPServer/`). The lambda
   signature `(const FHttpServerRequest&, const FHttpResultCallback&)` is the
   5.3+ form; older builds used a delegate. If `AddRoute` rejects it, switch to
   `Server.OnRequest().AddRaw(...)` style.

5. **`AutoMeshRender.cpp` — `FCoreDelegates::OnEndFrame`**. Stable; but if you
   prefer, you can instead override `UGameInstance::Init` or a
   `UWorldSubsystem::Tick` to start the server. OnEndFrame is used here because
   it needs no extra asset/blueprint wiring.

## Layout

```
Source/AutoMeshRender/
  AutoMeshRender.h/.cpp   module entry; starts HTTP service on first frame
  RenderService.h/.cpp    HTTP listener + /reward route dispatch
  MeshComparator.h/.cpp   OBJ parse → ProceduralMesh → 6-view depth render → MSE
Config/DefaultEngine.ini  VRAM caps (512 RT, no Lumen/Nanite/RT)
AutoMeshRender.uproject   module + HTTPServer/JsonUtilities plugins
```

## Metric

Reward = `1 / (1 + MSE_depth)` over 6 cube-face views at 512². This is a
starting signal; upgrade `CompareBuffers` to SSIM/LPIPS (roadmap M2) without
touching the HTTP/import/camera plumbing.
