from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from automesh.mesh import Mesh


@dataclass(frozen=True)
class CollapseCandidate:
    edge: tuple[int, int]
    position: np.ndarray
    cost: float
    features: np.ndarray
    boundary: bool = False


@dataclass(frozen=True)
class CollapseResult:
    mesh: Mesh
    candidate: CollapseCandidate


class QEMSimplifier:
    """QEM edge-collapse helper used as an RL action generator."""

    def __init__(
        self,
        boundary_weight: float = 10.0,
        normal_flip_threshold: float = 0.0,
        min_area: float = 1e-12,
    ) -> None:
        self.boundary_weight = boundary_weight
        self.normal_flip_threshold = normal_flip_threshold
        self.min_area = min_area

    def compute_quadrics(self, mesh: Mesh) -> np.ndarray:
        quadrics = np.zeros((mesh.vertex_count, 4, 4), dtype=np.float64)
        for face in mesh.faces:
            points = mesh.vertices[face]
            normal = np.cross(points[1] - points[0], points[2] - points[0])
            norm = np.linalg.norm(normal)
            if norm <= 1e-12:
                continue
            normal = normal / norm
            d = -float(np.dot(normal, points[0]))
            plane = np.array([normal[0], normal[1], normal[2], d], dtype=np.float64)
            q = np.outer(plane, plane)
            for vertex_index in face:
                quadrics[int(vertex_index)] += q

        if self.boundary_weight > 0.0:
            self._add_boundary_quadrics(mesh, quadrics)
        return quadrics

    def candidates(self, mesh: Mesh, top_k: int | None = None) -> list[CollapseCandidate]:
        quadrics = self.compute_quadrics(mesh)
        edge_faces = mesh.edge_face_adjacency()
        neighbors = mesh.vertex_neighbors()
        candidates: list[CollapseCandidate] = []

        for edge in mesh.unique_edges():
            u, v = int(edge[0]), int(edge[1])
            adjacent_faces = edge_faces.get((u, v), [])
            if len(adjacent_faces) not in (1, 2):
                continue
            common_neighbors = (neighbors[u] & neighbors[v]) - {u, v}
            if len(common_neighbors) != len(adjacent_faces):
                continue

            q = quadrics[u] + quadrics[v]
            position, cost = self._best_position(mesh.vertices[u], mesh.vertices[v], q)
            boundary = len(adjacent_faces) == 1
            candidate = CollapseCandidate(
                edge=(u, v),
                position=position,
                cost=cost,
                features=self._features(mesh, u, v, cost, boundary),
                boundary=boundary,
            )
            if self.is_legal(mesh, candidate):
                candidates.append(candidate)

        candidates.sort(key=lambda item: item.cost)
        if top_k is not None:
            return candidates[:top_k]
        return candidates

    def collapse(self, mesh: Mesh, candidate: CollapseCandidate) -> CollapseResult:
        if not self.is_legal(mesh, candidate):
            raise ValueError(f"illegal collapse for edge {candidate.edge}")
        u, v = candidate.edge
        vertices = mesh.vertices.copy()
        vertices[u] = candidate.position

        faces = mesh.faces.copy()
        faces[faces == v] = u
        valid = np.logical_and.reduce(
            [
                faces[:, 0] != faces[:, 1],
                faces[:, 1] != faces[:, 2],
                faces[:, 2] != faces[:, 0],
            ]
        )
        faces = faces[valid]
        faces = _remove_duplicate_faces(faces)
        collapsed = Mesh(vertices, faces).compact()
        return CollapseResult(mesh=collapsed, candidate=candidate)

    def is_legal(self, mesh: Mesh, candidate: CollapseCandidate) -> bool:
        u, v = candidate.edge
        if u == v or u < 0 or v < 0 or u >= mesh.vertex_count or v >= mesh.vertex_count:
            return False

        faces = mesh.faces
        incident_mask = np.any((faces == u) | (faces == v), axis=1)
        old_normals = mesh.face_normals()
        vertices = mesh.vertices.copy()
        vertices[u] = candidate.position

        seen_faces: set[tuple[int, int, int]] = set()
        for face_index, face in enumerate(faces):
            new_face = face.copy()
            new_face[new_face == v] = u
            if len(set(int(x) for x in new_face)) < 3:
                continue

            canonical = tuple(sorted(int(x) for x in new_face))
            if canonical in seen_faces:
                return False
            seen_faces.add(canonical)

            points = vertices[new_face]
            normal = np.cross(points[1] - points[0], points[2] - points[0])
            area2 = float(np.linalg.norm(normal))
            if area2 <= self.min_area:
                return False
            if incident_mask[face_index]:
                old_normal = old_normals[face_index]
                if np.linalg.norm(old_normal) > 1e-12:
                    new_normal = normal / area2
                    if float(np.dot(old_normal, new_normal)) < self.normal_flip_threshold:
                        return False
        return True

    def _best_position(self, a: np.ndarray, b: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, float]:
        lhs = q[:3, :3]
        rhs = -q[:3, 3]
        candidates = [a, b, 0.5 * (a + b)]
        try:
            if abs(float(np.linalg.det(lhs))) > 1e-12:
                candidates.append(np.linalg.solve(lhs, rhs))
        except np.linalg.LinAlgError:
            pass

        best_position = candidates[0]
        best_cost = float("inf")
        for position in candidates:
            homogeneous = np.array([position[0], position[1], position[2], 1.0], dtype=np.float64)
            cost = float(homogeneous @ q @ homogeneous)
            if cost < best_cost:
                best_position = position
                best_cost = cost
        return np.asarray(best_position, dtype=np.float64), best_cost

    def _features(self, mesh: Mesh, u: int, v: int, cost: float, boundary: bool) -> np.ndarray:
        neighbors = mesh.vertex_neighbors()
        edge_length = float(np.linalg.norm(mesh.vertices[u] - mesh.vertices[v]))
        valence_u = len(neighbors[u])
        valence_v = len(neighbors[v])
        return np.array(
            [
                cost,
                edge_length,
                float(valence_u),
                float(valence_v),
                1.0 if boundary else 0.0,
            ],
            dtype=np.float64,
        )

    def _add_boundary_quadrics(self, mesh: Mesh, quadrics: np.ndarray) -> None:
        edge_faces = mesh.edge_face_adjacency()
        normals = mesh.face_normals()
        for (u, v), adjacent_faces in edge_faces.items():
            if len(adjacent_faces) != 1:
                continue
            face_normal = normals[adjacent_faces[0]]
            edge_vector = mesh.vertices[v] - mesh.vertices[u]
            edge_length = np.linalg.norm(edge_vector)
            if edge_length <= 1e-12:
                continue
            edge_direction = edge_vector / edge_length
            boundary_normal = np.cross(edge_direction, face_normal)
            normal_length = np.linalg.norm(boundary_normal)
            if normal_length <= 1e-12:
                continue
            boundary_normal = boundary_normal / normal_length
            d = -float(np.dot(boundary_normal, mesh.vertices[u]))
            plane = np.array(
                [boundary_normal[0], boundary_normal[1], boundary_normal[2], d],
                dtype=np.float64,
            )
            q = self.boundary_weight * np.outer(plane, plane)
            quadrics[u] += q
            quadrics[v] += q


def _remove_duplicate_faces(faces: np.ndarray) -> np.ndarray:
    if faces.size == 0:
        return faces.reshape(0, 3)
    unique = []
    seen: set[tuple[int, int, int]] = set()
    for face in faces:
        key = tuple(sorted(int(x) for x in face))
        if key in seen:
            continue
        seen.add(key)
        unique.append(face)
    return np.asarray(unique, dtype=np.int64).reshape(-1, 3)
