#!/usr/bin/env bash
# Build AutoMeshRender against a UE5 install.
#
# Usage:
#   ./build.sh /path/to/UE5
#   ./build.sh /path/to/UE5 --config Debug
#
# The UE path must contain Engine/Build/BatchFiles/Linux/Build.sh (UBT wrapper).
# By default builds the AutoMeshRenderEditor target (Editor, uncooked dev runs),
# producing libUnrealEditor-AutoMeshRender.so. The Game target
# (AutoMeshRender.Target.cs) is cooked-only and not used in development.
set -euo pipefail

CONFIG="Development"
TARGET="AutoMeshRenderEditor"
EXTRA_ARGS=()

usage() {
  cat <<EOF
Usage: $0 <UE_ROOT> [options]

  <UE_ROOT>   Path to your UE5 source tree (contains Engine/Build/BatchFiles/...).

Options:
  --config CFG      Build config. Default: Development. (Do NOT use Shipping —
                    it drops runtime-module support the service relies on.)
  --target NAME     Target name. Default: AutoMeshRenderEditor (Editor target,
                    produces libUnrealEditor-AutoMeshRender.so for uncooked runs).
                    Pass an engine target (e.g. ShaderCompileWorker) WITHOUT
                    --project to build engine helper binaries.
  --no-project      Do not pass -Project= (for engine targets).
  --clean           Clean before building (passes -Clean to UBT).
  -h, --help        Show this help.
EOF
}

# --- parse args ---
if [[ $# -lt 1 ]]; then
  usage
  echo "error: UE_ROOT is required" >&2
  exit 2
fi

# Allow -h/--help before the positional UE_ROOT.
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

UE_ROOT="$1"; shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)  CONFIG="$2"; shift 2;;
    --target)  TARGET="$2"; shift 2;;
    --no-project) NO_PROJECT=1; shift;;
    --clean)   EXTRA_ARGS+=("-Clean"); shift;;
    -h|--help) usage; exit 0;;
    *) echo "error: unknown arg: $1" >&2; usage; exit 2;;
  esac
done

# --- validate UE_ROOT ---
UBT="$UE_ROOT/Engine/Build/BatchFiles/Linux/Build.sh"
if [[ ! -f "$UBT" ]]; then
  echo "error: UBT wrapper not found at: $UBT" >&2
  echo "       UE_ROOT should be the tree containing Engine/Build/BatchFiles/Linux/Build.sh" >&2
  echo "       (this is the same UE5 source dir you built the engine from)" >&2
  exit 2
fi

if [[ ! -x "$UBT" ]]; then
  chmod +x "$UBT" 2>/dev/null || true
fi

# --- derive project path (relative to this script) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$SCRIPT_DIR/AutoMeshRender.uproject"
if [[ ! -f "$PROJ" ]]; then
  echo "error: project file not found: $PROJ" >&2
  exit 2
fi

# --- config sanity (warn, don't block — let UBT be the source of truth) ---
if [[ "$CONFIG" == "Shipping" && "${NO_PROJECT:-0}" != "1" ]]; then
  echo "warning: Shipping config drops runtime-module support (HTTPServer, ProceduralMeshComponent)." >&2
  echo "         The render service will likely fail to link. Use Development." >&2
fi

# --- target file presence check (project targets only) ---
if [[ "${NO_PROJECT:-0}" != "1" ]]; then
  # Target files live in Source/ (NOT in the module dir), per UE convention —
  # putting them in Source/AutoMeshRender/ causes CS0101 class-duplication.
  TARGET_CS="$SCRIPT_DIR/Source/${TARGET}.Target.cs"
  if [[ ! -f "$TARGET_CS" ]]; then
    echo "error: target file not found: $TARGET_CS" >&2
    echo "       Expected ${TARGET}.Target.cs in Source/ (next to the module dir)" >&2
    echo "       Rule: file Xxx.Target.cs -> class XxxTarget -> target name Xxx" >&2
    echo "       Available project targets: AutoMeshRenderEditor (Editor/dev, default), AutoMeshRender (Game/cooked)" >&2
    exit 2
  fi
fi

# --- build ---
echo "==> UE_ROOT : $UE_ROOT"
echo "==> UBT     : $UBT"
echo "==> project : $PROJ"
echo "==> target  : $TARGET ($CONFIG)"
echo

BUILD_ARGS=("$TARGET" Linux "$CONFIG")
if [[ "${NO_PROJECT:-0}" != "1" ]]; then
  BUILD_ARGS+=("-Project=$PROJ")
fi
BUILD_ARGS+=("-WaitMutex")
BUILD_ARGS+=("${EXTRA_ARGS[@]}")

set +e
"$UBT" "${BUILD_ARGS[@]}"
RC=$?
set -e

echo
if [[ $RC -ne 0 ]]; then
  echo "error: build failed (exit $RC)" >&2
  echo "       Common causes:" >&2
  echo "       - include/type errors in MeshComparator.cpp or RenderService.cpp" >&2
  echo "         (see ue/AutoMeshRender/README.md §API notes for verified UE5.4 APIs)" >&2
  echo "       - engine built as Shipping; rebuild engine as Development" >&2
  echo "       - for engine targets (ShaderCompileWorker etc.) pass --no-project" >&2
  exit $RC
fi

# Editor targets produce a .so; Game/engine targets produce an executable.
if [[ -f "$SCRIPT_DIR/Binaries/Linux/libUnrealEditor-${TARGET}.so" ]]; then
  echo "==> OK: $SCRIPT_DIR/Binaries/Linux/libUnrealEditor-${TARGET}.so"
elif [[ -f "$SCRIPT_DIR/Binaries/Linux/$TARGET" ]]; then
  echo "==> OK: $SCRIPT_DIR/Binaries/Linux/$TARGET"
else
  echo "==> build returned success but no product found in Binaries/Linux/" >&2
  echo "    (engine targets like UnrealPak may not produce one here; this is fine)" >&2
fi
echo
echo "Next: runtime verification — see ue/AutoMeshRender/README.md §Run / §Verify"
