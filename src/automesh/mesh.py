from __future__ import annotations

from dataclasses import dataclass

import numpy as np


ArrayLike = np.ndarray


@dataclass(frozen=True)
class Mesh:
    """A triangle mesh with vertex positions and face indices."""

    vertices: ArrayLike
    faces: ArrayLike

    def __post_init__(self) -> None:
        vertices = np.asarray(self.vertices, dtype=np.float64)
        faces = np.asarray(self.faces, dtype=np.int64)
        if vertices.ndim != 2 or vertices.shape[1] != 3:
            raise ValueError("vertices must have shape (N, 3)")
        if faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("faces must have shape (M, 3)")
        if len(vertices) and len(faces):
            if faces.min() < 0 or faces.max() >= len(vertices):
                raise ValueError("faces contain vertex indices outside vertices")
        object.__setattr__(self, "vertices", vertices)
        object.__setattr__(self, "faces", faces)

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])

    @property
    def face_count(self) -> int:
        return int(self.faces.shape[0])

    def copy(self) -> "Mesh":
        return Mesh(self.vertices.copy(), self.faces.copy())

    def face_vertices(self) -> ArrayLike:
        return self.vertices[self.faces]

    def face_normals(self) -> ArrayLike:
        tris = self.face_vertices()
        normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        return np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 1e-12)

    def face_areas(self) -> ArrayLike:
        tris = self.face_vertices()
        cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        return 0.5 * np.linalg.norm(cross, axis=1)

    def unique_edges(self) -> ArrayLike:
        if self.face_count == 0:
            return np.empty((0, 2), dtype=np.int64)
        faces = self.faces
        edges = np.vstack(
            [
                faces[:, [0, 1]],
                faces[:, [1, 2]],
                faces[:, [2, 0]],
            ]
        )
        edges = np.sort(edges, axis=1)
        return np.unique(edges, axis=0)

    def edge_face_adjacency(self) -> dict[tuple[int, int], list[int]]:
        adjacency: dict[tuple[int, int], list[int]] = {}
        for face_index, face in enumerate(self.faces):
            for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
                edge = tuple(sorted((int(a), int(b))))
                adjacency.setdefault(edge, []).append(face_index)
        return adjacency

    def vertex_neighbors(self) -> list[set[int]]:
        neighbors = [set() for _ in range(self.vertex_count)]
        for a, b in self.unique_edges():
            ai = int(a)
            bi = int(b)
            neighbors[ai].add(bi)
            neighbors[bi].add(ai)
        return neighbors

    def compact(self) -> "Mesh":
        if self.face_count == 0:
            return Mesh(np.empty((0, 3), dtype=np.float64), np.empty((0, 3), dtype=np.int64))
        used = np.unique(self.faces.reshape(-1))
        remap = np.full(self.vertex_count, -1, dtype=np.int64)
        remap[used] = np.arange(len(used), dtype=np.int64)
        return Mesh(self.vertices[used], remap[self.faces])

    @staticmethod
    def cube(size: float = 2.0) -> "Mesh":
        s = size * 0.5
        vertices = np.array(
            [
                [-s, -s, -s],
                [s, -s, -s],
                [s, s, -s],
                [-s, s, -s],
                [-s, -s, s],
                [s, -s, s],
                [s, s, s],
                [-s, s, s],
            ],
            dtype=np.float64,
        )
        faces = np.array(
            [
                [0, 2, 1],
                [0, 3, 2],
                [4, 5, 6],
                [4, 6, 7],
                [0, 1, 5],
                [0, 5, 4],
                [1, 2, 6],
                [1, 6, 5],
                [2, 3, 7],
                [2, 7, 6],
                [3, 0, 4],
                [3, 4, 7],
            ],
            dtype=np.int64,
        )
        return Mesh(vertices, faces)

    @staticmethod
    def grid(width: int = 8, height: int = 8, size: float = 2.0) -> "Mesh":
        if width < 2 or height < 2:
            raise ValueError("grid width and height must be at least 2")
        xs = np.linspace(-size * 0.5, size * 0.5, width)
        ys = np.linspace(-size * 0.5, size * 0.5, height)
        vertices = np.array([[x, y, 0.0] for y in ys for x in xs], dtype=np.float64)
        faces = []
        for y in range(height - 1):
            for x in range(width - 1):
                a = y * width + x
                b = a + 1
                c = a + width
                d = c + 1
                faces.append([a, b, d])
                faces.append([a, d, c])
        return Mesh(vertices, np.asarray(faces, dtype=np.int64))
