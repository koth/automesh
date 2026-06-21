# Compiling AutoMeshRender on Linux

Headless UE5 render service that backs the `HttpRenderReward` photoreal reward
(M4). This doc covers **building the project** only; engine build is in
`docs/UNREAL_SETUP.md`, project architecture in `ue/AutoMeshRender/README.md`.

---

## 0. Prerequisites (must already be done)

| Step | Where | Check |
|---|---|---|
| UE5 source built, `Development` | `docs/UNREAL_SETUP.md` §2 | `ls ~/ue/UE5/Engine/Binaries/Linux/UnrealEditor` |
| `ShaderCompileWorker` built, `Development` | `docs/UNREAL_SETUP.md` §3 | `ls ~/ue/UE5/Engine/Binaries/Linux/ShaderCompileWorker` |
| GPU + Xvfb available | §1 of same doc | `nvidia-smi` + `which xvfb-run` |
| automesh repo on this machine | git clone | `ls ue/AutoMeshRender/AutoMeshRender.uproject` |

If any of these fail, stop and fix the engine first — you cannot build the
project without a working engine toolchain.

---

## 1. Environment

Set these once per shell session (or put in `~/.bashrc`):

```bash
export UE_ROOT="$HOME/ue/UE5"
export UBT="$UE_ROOT/Engine/Build/BatchFiles/Linux/Build.sh"
# Absolute path to the project on THIS machine:
export PROJ="$(pwd)/ue/AutoMeshRender/AutoMeshRender.uproject"
```

Verify the UBT wrapper exists:

```bash
test -f "$UBT" && echo "UBT ok" || echo "UBT missing — engine not built?"
```

---

## 2. Build the project (Editor target)

Build the **Editor target** (`AutoMeshRenderEditor`), not the Game target. The
Editor target produces `libUnrealEditor-AutoMeshRender.so`, which the engine
editor loads dynamically when you run uncooked in `-game` mode. The Game target
(`AutoMeshRender.Target.cs`) only builds a cooked-mode standalone binary and is
not used for development runs — see §5 for why.

```bash
"$UBT" AutoMeshRenderEditor Linux Development -Project="$PROJ" -WaitMutex
```

Or via the helper script (sets UBT/PROJ for you):

```bash
./ue/AutoMeshRender/build.sh "$UE_ROOT" --target AutoMeshRenderEditor
```

What each arg means:

| Arg | Meaning |
|---|---|
| `AutoMeshRenderEditor` | target name = prefix of `Source/AutoMeshRenderEditor.Target.cs` / class `AutoMeshRenderEditorTarget` (`Type = Editor`) |
| `Linux` | host platform (auto-detected, but explicit is safer) |
| `Development` | config — must match engine config, **not Shipping** (Shipping drops runtime module support) |
| `-Project=...` | path to this `.uproject`; without it UBT builds engine targets, not yours |
| `-WaitMutex` | avoid clashing with any other UBT instance (e.g. a running editor) |

### Expected output

```bash
ls -la ue/AutoMeshRender/Binaries/Linux/libUnrealEditor-AutoMeshRender.so
# A shared library. If it's missing, the build failed — see §4.
```

Wall time: 5-15 min the first time (compiles the module + links against engine),
1-2 min on incremental rebuilds.

---

## 3. Build the helper targets (only if asked for)

The first run may need these; build **only if** the engine complains they're
missing. Same `Build.sh` form, different target name, same `Development` config:

```bash
# Only if you get "ShaderCompileWorker not found" at runtime:
"$UBT" ShaderCompileWorker Linux Development -WaitMutex

# Only if packaging/load errors mention UnrealPak:
"$UBT" UnrealPak Linux Development -WaitMutex
```

These are engine targets, so **do not** pass `-Project=`.

---

## 4. Common build errors and fixes

### 4a. "Could not find ... target"

Cause: `Target.cs` missing or class name mismatch. Verify:

```bash
ls ue/AutoMeshRender/Source/AutoMeshRender.Target.cs
grep "class AutoMeshRenderTarget" ue/AutoMeshRender/Source/AutoMeshRender.Target.cs
```

Rule: file `Xxx.Target.cs` → class `XxxTarget` → target name `Xxx`. All three
must agree. **Target files live in `Source/` (not inside the module dir
`Source/AutoMeshRender/`)** — putting them there causes CS0101 class duplication
because UBT copies both Build.cs and Target.cs into the same Intermediate
compilation unit. Currently they're `AutoMeshRender.Target.cs` /
`AutoMeshRenderTarget` / `AutoMeshRender`.

### 4b. "No rule to make target" / `make` errors

You're invoking `make` instead of `Build.sh`. UE project builds go through UBT,
not the engine Makefile. Use the `"$UBT" ...` form in §2, not `make`.

### 4c. Include / type errors in `MeshComparator.cpp` or `RenderService.cpp`

These were the failure points during initial bring-up. All resolved against
the real UE5.4 headers on the build host — the current code in-tree compiles.
For reference, the corrections that were needed (in case of regressions):

- `#include "Components/SceneCaptureComponent2D.h"` (full path, not bare)
- `#include "ProceduralMeshComponent.h"` and `#include "KismetProceduralMeshLibrary.h"` (no `Components/` or `Kismet/` prefix — they're at the `ProceduralMeshComponent` plugin's `Public/` root)
- `#include "MaterialDomain.h"` for `EMaterialDomain` (forward-declared by `Material.h`)
- `#include "Misc/CoreDelegates.h"` for `FCoreDelegates::OnEndFrame`
- HTTP API: `FHttpServerModule::Get().GetHttpRouter(Port, bFailOnBindFailure)` → `IHttpRouter::BindRoute(...)`. No `IHttpServer`/`FHttpRouteHandler` in 5.4.
- `UTextureRenderTarget2D::InitAutoFormat(W,H)` (not `Init`)
- `UKismetProceduralMeshLibrary::CalculateTangentsForMesh(...)` (no `CalculateNormals`)
- `ReadSurfaceData` on `FRHICommandListImmediate` via `ENQUEUE_RENDER_COMMAND`, pixels are `FColor`
- `IMPLEMENT_PRIMARY_GAME_MODULE` (not `IMPLEMENT_MODULE`) for the primary game module
- No C++ `try/catch` (UE disables exceptions) — use out-param error reporting

Full verified-API list in `ue/AutoMeshRender/README.md` §"API notes".

### 4d. "module HTTPServer not found" / "Unable to find plugin 'HTTPServer'"

`HTTPServer` is an **engine module, not a plugin** — do NOT list it under
`.uproject` `Plugins` (that triggers "Unable to find plugin 'HTTPServer'").
It is declared as a build dependency in `AutoMeshRender.Build.cs`
`PrivateDependencyModuleNames` (already present as `"HTTPServer"`, matching the
real engine directory `Engine/Source/Runtime/Online/HTTPServer/`). Module names
are case-sensitive on Linux and MUST match the directory name exactly — it is
`HTTPServer` (all caps), not `HttpServer`. The include is
`#include "HttpServerModule.h"` (file name is mixed case, that's fine).

### 4e. Mixed config link errors

Engine is `Development`, project must be `Development` too. Don't pass
`Shipping` to §2. If you built the engine as `Shipping`, rebuild the engine as
`Development` first (`docs/UNREAL_SETUP.md` §2).

---

## 5. After a successful build — run uncooked via the editor

Run with the **engine editor in `-game` mode**, not the Game-target binary.
The editor loads uncooked content and compiles shaders online, so no
`Content/` pak or cook step is needed. The Game-target binary runs in cooked
mode and fatal-exits with "Failed to initialize ShaderCodeLibrary" or
"No COOKED content was found" without a full packaging pass.

```bash
xvfb-run -a -s "-screen 0 1280x720x24" \
  "$UE_ROOT/Engine/Binaries/Linux/UnrealEditor" \
  "$PROJ" \
  -game -RenderOffScreen -NoLoadStartupPackages -Unattended -NoSplash -NoPause \
  -windowed -resx=1 -resy=1 -RenderServicePort=8765 -log &

# wait for: [AutoMeshRender] HTTP service listening on 127.0.0.1:8765/reward and /render
# (first run spends a few minutes compiling shaders; fast once DDC is warm)

# then verify the endpoint (mesh content travels in the body):
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"original_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","current_obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","step":0,"faces":1}))')"
# -> {"reward": 1.0...}  (identical meshes)

# /render returns a PNG (single 3/4 view, 1024x1024 by default):
curl -s -X POST http://127.0.0.1:8765/render \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"obj":"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n","width":1024,"height":1024}))')" \
  -o /tmp/mesh.png
file /tmp/mesh.png   # -> PNG image data, 1024 x 1024
```

### Why not the Game binary

The Game target (`AutoMeshRender.Target.cs`, `Type = Game`) builds a standalone
executable that runs in **cooked mode**: it expects packaged `Content/`
(shader library paks, cooked assets). Two config items mitigate the shader
library init but only the editor fully avoids it:
- `Config/DefaultGame.ini` sets `bShareMaterialShaderCode=False` under
  `[/Script/UnrealEd.ProjectPackagingSettings]` — the `bArchive` flag read by
  `FShaderCodeLibrary::InitForRuntime`.
- Cooking needs `UnrealPak`/Project Launcher; skip it in dev by using the editor.

Build-time work is done here; runtime errors are a separate debugging pass.
See `ue/AutoMeshRender/README.md` §Run / §Verify for the canonical commands.
