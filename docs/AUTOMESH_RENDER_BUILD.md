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

## 2. Build the project

One command. UBT generates project files, compiles the `AutoMeshRender` module,
and links the `AutoMeshRender` target defined in
`Source/AutoMeshRender.Target.cs` (`Type = Game`).

```bash
"$UBT" AutoMeshRender Linux Development -Project="$PROJ" -WaitMutex
```

What each arg means:

| Arg | Meaning |
|---|---|
| `AutoMeshRender` | target name = prefix of `AutoMeshRender.Target.cs` / class `AutoMeshRenderTarget` |
| `Linux` | host platform (auto-detected, but explicit is safer) |
| `Development` | config — must match engine config, **not Shipping** (Shipping drops runtime module support) |
| `-Project=...` | path to this `.uproject`; without it UBT builds engine targets, not yours |
| `-WaitMutex` | avoid clashing with any other UBT instance (e.g. a running editor) |

### Expected output

```bash
ls -la ue/AutoMeshRender/Binaries/Linux/AutoMeshRender
# An executable, ~50-150 MB. If it's missing, the build failed — see §4.
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

These are the expected failure points — the C++ was written against public UE5.4
APIs but not compiled here. Most likely:

- `#include "SceneCaptureComponent2D.h"` path → try `Components/SceneCaptureComponent2D.h`
- `#include "KismetProceduralMeshLibrary.h"` → try `Kismet/KismetProceduralMeshLibrary.h`
- `FHttpRouteHandler` / `EHttpServerRequestVerbs` → `HttpServer` engine module API shift
- `ReadSurfaceData` signature → render-thread read API

Fix path: change the include to the full module-prefixed path UBT reports in the
error (e.g. `Components/SceneCaptureComponent2D.h`), or add the module to
`AutoMeshRender.Build.cs` `PrivateDependencyModuleNames` if the symbol comes
from a module not yet depended on. See `ue/AutoMeshRender/README.md` §"Known
compile-risk points" for the full list.

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

## 5. After a successful build

Proceed to runtime verification (next stage, documented in
`ue/AutoMeshRender/README.md` §Run / §Verify):

```bash
xvfb-run -a -s "-screen 0 1280x720x24" \
  ./ue/AutoMeshRender/Binaries/Linux/AutoMeshRender \
  /abs/automesh/ue/AutoMeshRender/AutoMeshRender.uproject \
  -game -RenderOffScreen -NoLoadStartupPackages -Unattended -NoSplash -NoPause \
  -windowed -resx=1 -resy=1 -RenderServicePort=8765 &

# then:
curl -s -X POST http://127.0.0.1:8765/reward \
  -H 'Content-Type: application/json' \
  -d '{"original":"/shared/o.obj","current":"/shared/c.obj","step":0,"faces":0}'
```

Build-time work is done here; runtime errors are a separate debugging pass.
