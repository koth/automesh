# AutoMeshRender — persistent headless UE5 render service

Server side of the render-service contract in `src/automesh/rewards.py`:

```
POST /reward  {"original_obj": <obj text>, "current_obj": <obj text>, "step": int, "faces": int}
  -> 200 {"reward": <float>}
POST /render  {"obj": <obj text>, "width": int=1024, "height": int=1024}
  -> 200 image/png            (single 3/4 shaded view of the mesh, auto-framed)
   | 200 application/json {"error": ...}   (bad mesh / render failure)
```

`/reward` is the sparse photoreal reward (Roadmap M4); `/render` renders a
single mesh to a PNG for inspection / paper figures (Roadmap M5). Meshes travel
as OBJ text in the request body, so the service needs **no shared filesystem** —
it runs from a single HTTP request. The clients (`HttpRenderReward` and
`render_mesh_image` in `src/automesh/rewards.py`) serialize meshes with
`mesh_to_obj_string`.

## Build

Requires UE5 built from source (`docs/UNREAL_SETUP.md`), with `UnrealEditor` +
`ShaderCompileWorker` in `Development`. Build the **Editor target**
(`AutoMeshRenderEditor`) — this produces `libUnrealEditor-AutoMeshRender.so`,
which the engine editor can dynamically load. The Game target
(`AutoMeshRender.Target.cs`) only builds a cooked-mode standalone binary and is
not used for development runs.

```bash
# Build the Editor target via the helper script:
./ue/AutoMeshRender/build.sh /path/to/UE5 --target AutoMeshRenderEditor

# Or directly with UBT:
/path/to/UE5/Engine/Build/BatchFiles/Linux/Build.sh \
    AutoMeshRenderEditor Linux Development \
    -Project="/abs/automesh/ue/AutoMeshRender/AutoMeshRender.uproject" -WaitMutex
# Output: ue/AutoMeshRender/Binaries/Linux/libUnrealEditor-AutoMeshRender.so
```

Notes:
- `-Project=` is required so UBT uses this `.uproject`, not the engine's.
- Target file naming: `Source/AutoMeshRenderEditor.Target.cs` (in `Source/`,
  not in the module dir) → class `AutoMeshRenderEditorTarget` → target name
  `AutoMeshRenderEditor`. See `docs/AUTOMESH_RENDER_BUILD.md` §4a.
- Two targets exist: `AutoMeshRender` (Game, cooked) and `AutoMeshRenderEditor`
  (Editor, uncooked dev). Build the Editor one for running the service.

## Run

Run with the **engine editor in `-game` mode** — it loads uncooked content and
compiles shaders online, so no `Content/` pak or cook step is needed. The
service module's `StartupModule` hooks `OnEndFrame` and starts the HTTP
listener on the first frame with a viewport.

```bash
xvfb-run -a -s "-screen 0 1280x720x24" \
  /path/to/UE5/Engine/Binaries/Linux/UnrealEditor \
  /abs/automesh/ue/AutoMeshRender/AutoMeshRender.uproject \
  -game -RenderOffScreen -NoLoadStartupPackages -Unattended -NoSplash -NoPause \
  -windowed -resx=1 -resy=1 -RenderServicePort=8765 -log
```

Wait for `[AutoMeshRender] HTTP service listening on 127.0.0.1:8765/reward and /render` in
the log (first run spends a few minutes compiling shaders; subsequent runs are
fast once the DDC is warm).

## Verify

```bash
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"original_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","current_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","step":0,"faces":1}))')"
# -> {"reward": 1.0...}  (identical meshes)

# /render returns a PNG; save it and check the signature.
curl -s -X POST http://127.0.0.1:8765/render \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","width":1024,"height":1024}))')" \
  -o /tmp/mesh.png
file /tmp/mesh.png   # -> PNG image data, 1024 x 1024
```

Or from the CLI (writes the PNG to disk):

```bash
automesh render-image input.obj out.png --endpoint http://127.0.0.1:8765/render
```

## Why editor, not the Game binary

The Game target produces a standalone executable that runs in **cooked mode**:
it expects packaged `Content/` (shader library paks, cooked assets). A bare
`-game` dev run has no cooked content, so it fatal-exits with either
"Failed to initialize ShaderCodeLibrary" or "No COOKED content was found".
Cooking requires the full `UnrealPak`/Project Launcher packaging flow, which is
unnecessary for development. The editor's `-game` mode (Standalone Game) loads
uncooked content and compiles shaders on demand, avoiding both errors.

For reference, the two shader-library gotchas that block the Game binary are
handled in config but only fully resolved by using the editor:
- `Config/DefaultGame.ini` sets `bShareMaterialShaderCode=False` under
  `[/Script/UnrealEd.ProjectPackagingSettings]` — this is the `bArchive` flag
  read by `FShaderCodeLibrary::InitForRuntime` (ShaderCodeLibrary.cpp).
- `Config/DefaultEngine.ini` has the VRAM caps but no longer carries the
  ineffective `r.ShaderCodeLibrary.*` console vars (they run after the fatal
  init and don't prevent it).

## Layout

```
Source/
  AutoMeshRenderEditor.Target.cs   Editor target (build this one)
  AutoMeshRender.Target.cs         Game target (cooked-only, not used in dev)
  AutoMeshRender/
    AutoMeshRender.Build.cs        deps: HTTPServer, Json, ProceduralMeshComponent, ImageWrapper, ...
    AutoMeshRender.h/.cpp          module entry (IMPLEMENT_PRIMARY_GAME_MODULE) + OnEndFrame
    RenderService.h/.cpp           HTTP listener via IHttpRouter + /reward and /render routes
    MeshComparator.h/.cpp          OBJ text parse → ProceduralMesh; /reward: 6-view depth → MSE; /render: single 3/4 view → PNG
Config/
  DefaultEngine.ini                VRAM caps (512 RT, no Lumen/Nanite/RT)
  DefaultGame.ini                  bShareMaterialShaderCode=False (shader lib off)
AutoMeshRender.uproject            module declaration (HTTPServer is an engine module, not a plugin)
build.sh                           build helper (takes UE_ROOT, --target, --config)
```

## Metric

Reward = `1 / (1 + MSE_depth)` over 6 cube-face views at 512². Starting signal;
upgrade `CompareBuffers` to SSIM/LPIPS (roadmap M2) without touching the
HTTP/import/camera plumbing.

`/render` returns a single auto-framed 3/4 view at 1024² (overridable via
`width`/`height`, clamped to ≤4096). Capture source is `SCS_FinalColorLDR`
(visible shaded image, not depth). PNG encoded with the `ImageWrapper` module.

## API notes (verified against UE5.4)

These were resolved by reading the actual engine headers on the build host:
- `HTTPServer` is an engine module at `Engine/Source/Runtime/Online/HTTPServer/`
  (case-sensitive `HTTPServer`, not `HttpServer`). Declared as a build dep in
  `Build.cs`, NOT a `.uproject` plugin.
- HTTP server API: `FHttpServerModule::Get().GetHttpRouter(Port, bFailOnBindFailure)`
  → `IHttpRouter::BindRoute(FHttpPath, EHttpServerRequestVerbs, FHttpRequestHandler)`.
  No `IHttpServer`/`FHttpRouteHandler` — those don't exist in 5.4.
- `UTextureRenderTarget2D::InitAutoFormat(W,H)` (not `Init`).
- `UKismetProceduralMeshLibrary::CalculateTangentsForMesh` (no `CalculateNormals`).
- `ReadSurfaceData` is on `FRHICommandList` (via `ENQUEUE_RENDER_COMMAND` with
  `FRHICommandListImmediate&`), pixels are `FColor` not `FLinearColor`.
- `EMaterialDomain` needs `#include "MaterialDomain.h"`.
- `ProceduralMeshComponent.h` include has no `Components/` prefix.
- `IMPLEMENT_PRIMARY_GAME_MODULE` (not `IMPLEMENT_MODULE`) for the primary game module.
- UE disables C++ exceptions — no try/catch; report errors via out-params.
- PNG encoding: `IImageWrapperModule` (module name `ImageWrapper`, added to
  `Build.cs` Private deps) → `CreateImageWrapper(EImageFormat::PNG)` (note: it's
  `Create`, not `FindOrCreate`, in 5.4) → `SetRaw(Data, Size, W, H,
  ERGBFormat::BGRA, 8)` (FColor is BGRA in memory) → `GetCompressed(0)` returns a
  `TArray64<uint8>` (int32 quality arg; copy by pointer+count into `TArray<uint8>`).
- `/render` capture source is `SCS_FinalColorLDR` (final colour, not depth);
  `/reward` uses `SCS_SceneDepth`. Both reuse `USceneCaptureComponent2D`.
