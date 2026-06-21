from __future__ import annotations

from pathlib import Path

import numpy as np

from automesh.mesh import Mesh


def load_obj(path: str | Path) -> Mesh:
    """Load vertex positions and triangulated faces from a Wavefront OBJ file."""

    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts[0] == "v" and len(parts) >= 4:
                vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif parts[0] == "f" and len(parts) >= 4:
                indices = [_parse_obj_index(token, len(vertices)) for token in parts[1:]]
                for i in range(1, len(indices) - 1):
                    faces.append([indices[0], indices[i], indices[i + 1]])
    return Mesh(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64))


def save_obj(mesh: Mesh, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for vertex in mesh.vertices:
            handle.write(f"v {vertex[0]:.9g} {vertex[1]:.9g} {vertex[2]:.9g}\n")
        for face in mesh.faces:
            a, b, c = face + 1
            handle.write(f"f {a} {b} {c}\n")


def _parse_obj_index(token: str, vertex_count: int) -> int:
    raw = token.split("/")[0]
    index = int(raw)
    if index < 0:
        return vertex_count + index
    return index - 1
