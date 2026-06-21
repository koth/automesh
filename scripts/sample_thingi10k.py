"""Sample a diverse subset of meshes from a local Thingi10K dump.

Thingi10K ships as a large archive of models in mixed formats (STL, OBJ, OFF,
PLY, ...). This script scans a directory you have already unpacked, recovers
real mesh topology (welding STL's duplicated vertices so manifold / boundary /
connectivity stats are meaningful), stratifies the models into face-count
buckets, and writes a manifest.csv describing the selected subset. It does no
network access on purpose; download the archive separately.

Stratification
--------------
Face-count buckets (inclusive lower bound):

    tiny    < 1k         CI smoke tests, debugging
    small   1k - 10k     default policy comparisons
    medium  10k - 50k    render-aware reward runs
    large   50k - 200k   scalability benchmarks
    huge    > 200k       stress / outlier handling

With --diversify, each bucket is further split into manifold classes
(closed / open / nonmanifold) and sampled round-robin so the subset also
spans topology, not just size.

Usage
-----
    # unpack Thingi10K somewhere first, then:
    python scripts/sample_thingi10k.py --input-dir /data/Thingi10K --output manifest.csv --count 50
    python scripts/sample_thingi10k.py --input-dir /data/Thingi10K --per-bucket 10 --diversify --seed 7
"""

from __future__ import annotations

import argparse
import csv
import random
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BUCKETS: list[tuple[str, int, int]] = [
    ("tiny", 0, 1_000),
    ("small", 1_000, 10_000),
    ("medium", 10_000, 50_000),
    ("large", 50_000, 200_000),
    ("huge", 200_000, 10**9),
]

MESH_SUFFIXES = (".obj", ".stl", ".off", ".ply")


@dataclass
class MeshStats:
    path: Path
    fmt: str
    faces: int
    vertices: int
    edges: int
    components: int
    boundary_edges: int
    max_edge_share: int
    bbox_diag: float
    face_area_mean: float
    face_area_std: float
    status: str
    detail: str = ""

    @property
    def bucket(self) -> str:
        for name, lo, hi in BUCKETS:
            if lo <= self.faces < hi:
                return name
        return "unknown"

    @property
    def manifold(self) -> bool:
        return self.max_edge_share <= 2

    @property
    def closed(self) -> bool:
        return self.boundary_edges == 0

    @property
    def manifold_class(self) -> str:
        if self.max_edge_share > 2:
            return "nonmanifold"
        return "closed" if self.boundary_edges == 0 else "open"

    def as_row(self, root: Path) -> dict:
        try:
            rel = str(self.path.relative_to(root))
        except ValueError:
            rel = str(self.path)
        return {
            "id": self.path.stem,
            "path": rel,
            "format": self.fmt,
            "bucket": self.bucket,
            "faces": self.faces,
            "vertices": self.vertices,
            "edges": self.edges,
            "components": self.components,
            "boundary_edges": self.boundary_edges,
            "max_edge_share": self.max_edge_share,
            "manifold": int(self.manifold),
            "closed": int(self.closed),
            "manifold_class": self.manifold_class,
            "bbox_diag": f"{self.bbox_diag:.6g}",
            "face_area_mean": f"{self.face_area_mean:.6g}",
            "face_area_std": f"{self.face_area_std:.6g}",
            "status": self.status,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Mesh loading (lightweight, multi-format; only needs vertices + triangle faces)
# ---------------------------------------------------------------------------


@dataclass
class RawMesh:
    vertices: np.ndarray  # (N, 3) float64
    faces: np.ndarray  # (M, 3) int64
    fmt: str


def load_mesh(path: Path) -> RawMesh:
    suffix = path.suffix.lower()
    if suffix == ".obj":
        return _load_obj(path)
    if suffix == ".stl":
        return _load_stl(path)
    if suffix == ".off":
        return _load_off(path)
    if suffix == ".ply":
        return _load_ply(path)
    raise ValueError(f"unsupported format: {suffix}")


def _triangulate(face: list[int]) -> list[list[int]]:
    """Fan-triangulate a polygon face into triangles."""
    return [[face[0], face[i], face[i + 1]] for i in range(1, len(face) - 1)]


def _load_obj(path: Path) -> RawMesh:
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts[0] == "v" and len(parts) >= 4:
                vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif parts[0] == "f" and len(parts) >= 4:
                idx = [int(tok.split("/")[0]) for tok in parts[1:]]
                idx = [i - 1 if i > 0 else len(vertices) + i for i in idx]
                faces.extend(_triangulate(idx))
    return RawMesh(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64), "obj")


def _load_stl(path: Path) -> RawMesh:
    data = path.read_bytes()
    # Binary STL: header(80) + uint32 face count + 50 bytes/face.
    if len(data) >= 84:
        n_faces = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + 50 * n_faces:
            verts = np.empty((n_faces * 3, 3), dtype=np.float64)
            offset = 84
            for i in range(n_faces):
                # Each facet: 12 bytes normal + 3 * 12 bytes vertices + 2 bytes attr.
                v0 = struct.unpack_from("<3f", data, offset + 12)
                v1 = struct.unpack_from("<3f", data, offset + 24)
                v2 = struct.unpack_from("<3f", data, offset + 36)
                verts[i * 3] = v0
                verts[i * 3 + 1] = v1
                verts[i * 3 + 2] = v2
                offset += 50
            faces = np.arange(n_faces * 3, dtype=np.int64).reshape(-1, 3)
            return RawMesh(verts, faces, "stl")
    # ASCII STL fallback.
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        current: list[int] = []
        for raw in handle:
            parts = raw.split()
            if len(parts) == 4 and parts[0] == "vertex":
                vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
                current.append(len(vertices) - 1)
                if len(current) == 3:
                    faces.append(current)
                    current = []
    return RawMesh(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64), "stl")


def _load_off(path: Path) -> RawMesh:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        first = handle.readline().strip()
        if first.upper().startswith("OFF"):
            first = first[3:].strip()
        while first.startswith("#") or not first:
            first = handle.readline().strip()
        nv, nf, _ = (int(x) for x in first.split()[:3])
        vertices = np.asarray(
            [[float(x) for x in handle.readline().split()[:3]] for _ in range(nv)],
            dtype=np.float64,
        )
        faces: list[list[int]] = []
        for _ in range(nf):
            parts = handle.readline().split()
            count = int(parts[0])
            poly = [int(x) for x in parts[1 : 1 + count]]
            faces.extend(_triangulate(poly))
    return RawMesh(vertices, np.asarray(faces, dtype=np.int64), "off")


def _load_ply(path: Path) -> RawMesh:
    """Load PLY. Parses the common ASCII case with x/y/z floats and tri lists;
    exotic binary layouts fall back to header-only face/vertex counts."""
    with path.open("rb") as handle:
        header_lines: list[str] = []
        while True:
            line = handle.readline()
            if not line:
                break
            header_lines.append(line.decode("ascii", errors="replace").strip())
            if line.strip() == b"end_header":
                break
        nv = nf = 0
        fmt = "ascii"
        vertex_props: list[str] = []
        in_vertex = False
        for line in header_lines:
            toks = line.split()
            if not toks:
                continue
            if toks[0] == "format":
                fmt = toks[1]
            elif toks[0] == "element" and toks[1] == "vertex":
                nv = int(toks[2])
                in_vertex = True
            elif toks[0] == "element" and toks[1] == "face":
                nf = int(toks[2])
                in_vertex = False
            elif toks[0] == "property" and in_vertex:
                vertex_props.append(toks[-1])
        xyz_ok = {"x", "y", "z"}.issubset(set(vertex_props)) and len(vertex_props) == 3
        if fmt == "ascii" and xyz_ok:
            vertices = np.empty((nv, 3), dtype=np.float64)
            for i in range(nv):
                vertices[i] = [float(x) for x in handle.readline().split()[:3]]
            faces: list[list[int]] = []
            for _ in range(nf):
                parts = handle.readline().split()
                count = int(parts[0])
                poly = [int(x) for x in parts[1 : 1 + count]]
                faces.extend(_triangulate(poly))
            return RawMesh(vertices, np.asarray(faces, dtype=np.int64), "ply")
        verts = np.zeros((nv, 3), dtype=np.float64)
        return RawMesh(verts, np.zeros((nf, 3), dtype=np.int64), "ply")


# ---------------------------------------------------------------------------
# Topology recovery + feature extraction
# ---------------------------------------------------------------------------


def weld_vertices(vertices: np.ndarray, faces: np.ndarray, tol: float = 1e-7) -> tuple[np.ndarray, np.ndarray]:
    """Merge coincident vertices (essential for STL) by quantizing coordinates."""
    if len(vertices) == 0:
        return vertices, faces
    scaled = np.round(vertices / tol).astype(np.int64)
    uniq, inv = np.unique(scaled, axis=0, return_inverse=True)
    new_vertices = uniq.astype(np.float64) * tol
    return new_vertices, inv[faces].astype(np.int64)


def edge_stats(faces: np.ndarray) -> tuple[int, int, int]:
    """Return (unique_edges, boundary_edges, max_edge_share)."""
    if len(faces) == 0:
        return 0, 0, 0
    edges = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges.sort(axis=1)
    n = int(faces.max()) + 1
    keys = edges[:, 0].astype(np.int64) * (n + 1) + edges[:, 1].astype(np.int64)
    _, counts = np.unique(keys, return_counts=True)
    return int(counts.size), int((counts == 1).sum()), int(counts.max())


def component_count(vertices: int, faces: np.ndarray) -> int:
    if vertices == 0:
        return 0
    parent = list(range(vertices))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for tri in faces:
        union(int(tri[0]), int(tri[1]))
        union(int(tri[1]), int(tri[2]))
    return len({find(i) for i in range(vertices)})


def face_areas(vertices: np.ndarray, faces: np.ndarray) -> tuple[float, float]:
    if len(faces) == 0:
        return 0.0, 0.0
    tris = vertices[faces]
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    return float(areas.mean()), float(areas.std())


def analyze(path: Path, weld: bool) -> MeshStats:
    raw = load_mesh(path)
    vertices, faces = raw.vertices, raw.faces
    if weld and len(faces):
        vertices, faces = weld_vertices(vertices, faces)
    nv = int(vertices.shape[0])
    nf = int(faces.shape[0])
    edges, boundary, max_share = edge_stats(faces)
    components = component_count(nv, faces)
    if nv:
        span = vertices.max(axis=0) - vertices.min(axis=0)
        diag = float(np.linalg.norm(span))
    else:
        diag = 0.0
    area_mean, area_std = face_areas(vertices, faces)
    return MeshStats(
        path=path,
        fmt=raw.fmt,
        faces=nf,
        vertices=nv,
        edges=edges,
        components=components,
        boundary_edges=boundary,
        max_edge_share=max_share,
        bbox_diag=diag,
        face_area_mean=area_mean,
        face_area_std=area_std,
        status="ok",
    )


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*") if p.suffix.lower() in MESH_SUFFIXES]
    files.sort()
    return files


def stratified_sample(
    stats: list[MeshStats],
    *,
    per_bucket: int | None,
    total: int,
    diversify: bool,
    seed: int,
) -> list[MeshStats]:
    rng = random.Random(seed)
    by_bucket: dict[str, list[MeshStats]] = {name: [] for name, _, _ in BUCKETS}
    for s in stats:
        if s.bucket in by_bucket:
            by_bucket[s.bucket].append(s)

    if per_bucket is not None:
        quota = {name: per_bucket for name, _, _ in BUCKETS}
    else:
        non_empty = [name for name, _, _ in BUCKETS if by_bucket[name]]
        base, remainder = divmod(total, max(len(non_empty), 1))
        quota = {name: base for name in non_empty}
        for name in non_empty[:remainder]:
            quota[name] += 1

    chosen: list[MeshStats] = []
    for name, _, _ in BUCKETS:
        pool = list(by_bucket[name])
        rng.shuffle(pool)
        want = quota.get(name, 0)
        if not diversify or want == 0:
            chosen.extend(pool[:want])
            continue
        by_class: dict[str, list[MeshStats]] = {}
        for s in pool:
            by_class.setdefault(s.manifold_class, []).append(s)
        classes = [c for c in by_class.values() if c]
        picked: list[MeshStats] = []
        idx = 0
        while len(picked) < want and classes:
            group = classes[idx % len(classes)]
            if group:
                picked.append(group.pop())
            idx += 1
        chosen.extend(picked)
    return chosen


def write_manifest(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id", "path", "format", "bucket", "faces", "vertices", "edges",
        "components", "boundary_edges", "max_edge_share", "manifold", "closed",
        "manifold_class", "bbox_diag", "face_area_mean", "face_area_std",
        "status", "detail",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input-dir", type=Path, required=True, help="Unpacked Thingi10K root directory.")
    parser.add_argument("--output", type=Path, default=Path("manifest.csv"), help="Manifest CSV output path.")
    parser.add_argument("--per-bucket", type=int, default=None, help="Fixed count per size bucket (overrides --count).")
    parser.add_argument("--count", type=int, default=50, help="Total count when --per-bucket is unset.")
    parser.add_argument("--diversify", action="store_true", help="Round-robin across manifold classes within each bucket.")
    parser.add_argument("--no-weld", action="store_true", help="Skip vertex welding (topology stats on STL will be degenerate).")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for reproducible sampling.")
    parser.add_argument("--max-files", type=int, default=None, help="Cap number of files scanned (for smoke tests).")
    parser.add_argument("--list-only", action="store_true", help="Write manifest for ALL files without sampling.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root: Path = args.input_dir
    if not root.is_dir():
        print(f"input dir not found: {root}", file=sys.stderr)
        return 2

    files = discover(root)
    if args.max_files is not None:
        files = files[: args.max_files]
    print(f"Scanning {len(files)} mesh files under {root} ...", file=sys.stderr)

    stats: list[MeshStats] = []
    errors = 0
    for i, path in enumerate(files, 1):
        try:
            stats.append(analyze(path, weld=not args.no_weld))
        except Exception as exc:  # noqa: BLE001 - keep scanning on bad files
            errors += 1
            stats.append(
                MeshStats(
                    path=path, fmt=path.suffix.lstrip(".").lower(), faces=0, vertices=0,
                    edges=0, components=0, boundary_edges=0, max_edge_share=0,
                    bbox_diag=0.0, face_area_mean=0.0, face_area_std=0.0,
                    status="error", detail=f"{exc.__class__.__name__}: {exc}",
                )
            )
        if i % 200 == 0:
            print(f"  ...{i}/{len(files)}", file=sys.stderr)

    ok_stats = [s for s in stats if s.status == "ok"]
    print(f"Analyzed {len(ok_stats)} ok / {errors} error / {len(stats)} total.", file=sys.stderr)

    if not args.list_only:
        ok_stats = stratified_sample(
            ok_stats,
            per_bucket=args.per_bucket,
            total=args.count,
            diversify=args.diversify,
            seed=args.seed,
        )
        print(f"Selected {len(ok_stats)} entries.", file=sys.stderr)
        counts: dict[str, int] = {}
        for s in ok_stats:
            counts[s.bucket] = counts.get(s.bucket, 0) + 1
        for name, _, _ in BUCKETS:
            if name in counts:
                print(f"  {name}: {counts[name]}", file=sys.stderr)

    rows = [s.as_row(root) for s in ok_stats]
    write_manifest(rows, args.output)
    print(f"Wrote manifest: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
