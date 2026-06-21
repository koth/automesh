"""Smoke-test the UE render service by POSTing a mesh to /render.

Two ways to get a mesh:

  1. --input path/to.obj          load an existing Wavefront OBJ
  2. (no --input)                 generate one procedurally (cube / sphere /
                                  torus / cube-simplified) so you can verify the
                                  service without any asset files on hand.

The script POSTs the mesh to <endpoint>/render (default
http://127.0.0.1:8765/render), waits for the PNG, and writes it to
--output (default: out/render_test_<label>.png). With --save-obj the OBJ that
actually went over the wire is also written to disk, useful for debugging
serialization issues.

Examples
--------
    # Auto-generate a cube and render it
    python scripts/render_test.py

    # Render a specific OBJ
    python scripts/render_test.py --input out/sample.obj --output out/sample.png

    # Generate a sphere, also keep the OBJ for inspection
    python scripts/render_test.py --shape sphere --shape-args radius=1.2,segments=48 \
        --save-obj out/sphere.obj --output out/sphere.png

    # Render against a non-default endpoint, larger image
    python scripts/render_test.py --shape torus \
        --endpoint http://ue-box:9001/render --width 2048 --height 2048
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from automesh.io import load_obj, mesh_to_obj_string
from automesh.mesh import Mesh


# Add repo root to sys.path so `python scripts/render_test.py` works without an
# editable install. Keeps this script usable in CI / from a clean clone.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


DEFAULT_ENDPOINT = "http://127.0.0.1:8765/render"
DEFAULT_OUTPUT_DIR = _REPO_ROOT / "out"


@dataclass
class RenderRequest:
    """A mesh ready to send, plus a label for naming the output."""

    obj_text: str
    label: str
    face_count: int
    source: str  # "input:<path>" or "generated:<shape>"


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------
def make_cube(size: float = 2.0) -> Mesh:
    return Mesh.cube(size=size)


def make_sphere(radius: float = 1.0, segments: int = 32) -> Mesh:
    """UV-sphere with triangles (not quad-pairs split) so it's watertight."""
    segments = max(3, int(segments))
    vertices: list[tuple[float, float, float]] = []
    for i in range(segments + 1):
        # theta from 0 (north pole) to pi (south pole)
        theta = np.pi * i / segments
        for j in range(segments):
            phi = 2.0 * np.pi * j / segments
            x = float(radius * np.sin(theta) * np.cos(phi))
            y = float(radius * np.sin(theta) * np.sin(phi))
            z = float(radius * np.cos(theta))
            vertices.append((x, y, z))

    faces: list[tuple[int, int, int]] = []
    ring = segments
    for i in range(segments):
        for j in range(segments):
            a = i * ring + j
            b = i * ring + (j + 1) % segments
            c = (i + 1) * ring + (j + 1) % segments
            d = (i + 1) * ring + j
            if i == 0:
                # top cap
                faces.append((a, c, b))
            elif i == segments - 1:
                # bottom cap
                faces.append((a, d, c))
            else:
                faces.append((a, c, b))
                faces.append((a, d, c))
    verts = np.asarray(vertices, dtype=np.float64)
    tris = np.asarray(faces, dtype=np.int64)
    return Mesh(verts, tris)


def make_torus(major_radius: float = 1.0, minor_radius: float = 0.35,
               major_segments: int = 48, minor_segments: int = 24) -> Mesh:
    major_segments = max(3, int(major_segments))
    minor_segments = max(3, int(minor_segments))
    vertices: list[tuple[float, float, float]] = []
    for i in range(major_segments):
        theta = 2.0 * np.pi * i / major_segments
        ct, st = np.cos(theta), np.sin(theta)
        for j in range(minor_segments):
            phi = 2.0 * np.pi * j / minor_segments
            cp, sp = np.cos(phi), np.sin(phi)
            r = major_radius + minor_radius * cp
            x = float(r * ct)
            y = float(r * st)
            z = float(minor_radius * sp)
            vertices.append((x, y, z))
    faces: list[tuple[int, int, int]] = []
    ring = minor_segments
    for i in range(major_segments):
        i2 = (i + 1) % major_segments
        for j in range(minor_segments):
            j2 = (j + 1) % minor_segments
            a = i * ring + j
            b = i * ring + j2
            c = i2 * ring + j2
            d = i2 * ring + j
            faces.append((a, b, c))
            faces.append((a, c, d))
    return Mesh(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64))


def make_cube_simplified(steps: int = 8) -> Mesh:
    """A cube passed through a few QEM collapses — a non-trivial test mesh."""
    from automesh.qem import QEMSimplifier

    steps = max(0, int(steps))
    mesh = Mesh.cube()
    simplifier = QEMSimplifier()
    for _ in range(steps):
        cands = simplifier.candidates(mesh, top_k=1)
        if not cands:
            break
        mesh = simplifier.collapse(mesh, cands[0]).mesh
    return mesh


GENERATORS = {
    "cube": make_cube,
    "sphere": make_sphere,
    "torus": make_torus,
    "cube-simplified": make_cube_simplified,
}


def _parse_shape_args(raw: str | None) -> dict[str, float]:
    """Parse "key=val,key=val" into {"key": float(...)}."""
    if not raw:
        return {}
    out: dict[str, float] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"shape-arg '{item}' is not key=value")
        key, val = item.split("=", 1)
        out[key.strip()] = float(val)
    return out


def _build_request(args: argparse.Namespace) -> RenderRequest:
    if args.input is not None:
        path = Path(args.input)
        mesh = load_obj(path)
        return RenderRequest(
            obj_text=mesh_to_obj_string(mesh),
            label=path.stem,
            face_count=mesh.face_count,
            source=f"input:{path}",
        )

    shape = args.shape
    if shape not in GENERATORS:
        raise SystemExit(
            f"unknown shape '{shape}'. choices: {', '.join(GENERATORS)}"
        )
    kwargs = _parse_shape_args(args.shape_args)
    mesh = GENERATORS[shape](**kwargs) if kwargs else GENERATORS[shape]()
    return RenderRequest(
        obj_text=mesh_to_obj_string(mesh),
        label=f"{shape}" + (f"_{'-'.join(f'{k}{v:g}' for k,v in sorted(kwargs.items()))}" if kwargs else ""),
        face_count=mesh.face_count,
        source=f"generated:{shape}({kwargs})",
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def post_render(req: RenderRequest, endpoint: str, width: int, height: int,
                timeout: float, user_agent: str) -> tuple[bytes, dict[str, str]]:
    payload = {"obj": req.obj_text, "width": int(width), "height": int(height)}
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
        method="POST",
    )
    headers_out: dict[str, str] = {}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                raise RuntimeError(f"render service returned HTTP {resp.status}")
            for k, v in resp.headers.items():
                headers_out[k] = v
            return resp.read(), headers_out
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"render service returned HTTP {exc.code}") from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Render a mesh via the UE render service /render endpoint.",
    )
    p.add_argument("--input", type=Path, default=None,
                   help="path to an input OBJ; if omitted a mesh is generated")
    p.add_argument("--shape", choices=sorted(GENERATORS), default="cube",
                   help="mesh to generate when --input is not set (default: cube)")
    p.add_argument("--shape-args", default=None,
                   help="comma-separated key=float args for the generator, e.g. "
                        "radius=1.2,segments=48")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                   help=f"render service /render URL (default: {DEFAULT_ENDPOINT})")
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--timeout", type=float, default=60.0,
                   help="HTTP timeout in seconds (warm calls should be much faster)")
    p.add_argument("--output", type=Path, default=None,
                   help="output PNG path (default: out/render_test_<label>.png)")
    p.add_argument("--save-obj", type=Path, default=None,
                   help="also write the OBJ that went over the wire to this path")
    p.add_argument("--user-agent", default="automesh-render-test/0.1")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    req = _build_request(args)

    out_path: Path
    if args.output is not None:
        out_path = args.output
    else:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_OUTPUT_DIR / f"render_test_{req.label}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.save_obj is not None:
        args.save_obj.parent.mkdir(parents=True, exist_ok=True)
        args.save_obj.write_text(req.obj_text, encoding="utf-8")

    if args.verbose:
        print(f"source     : {req.source}")
        print(f"faces      : {req.face_count}")
        print(f"obj bytes  : {len(req.obj_text)}")
        print(f"endpoint   : {args.endpoint}")
        print(f"resolution : {args.width}x{args.height}")
        print(f"output     : {out_path}")

    t0 = time.perf_counter()
    try:
        png_bytes, headers = post_render(
            req,
            endpoint=args.endpoint,
            width=args.width,
            height=args.height,
            timeout=args.timeout,
            user_agent=args.user_agent,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - t0

    ctype = headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            err = json.loads(png_bytes.decode("utf-8")).get("error", "render failed")
        except (ValueError, UnicodeDecodeError):
            err = "render failed"
        print(f"error: render service: {err}", file=sys.stderr)
        return 3

    out_path.write_bytes(png_bytes)
    print(
        f"saved {out_path} ({len(png_bytes)} bytes, "
        f"{args.width}x{args.height}, {elapsed:.2f}s, source={req.source})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
