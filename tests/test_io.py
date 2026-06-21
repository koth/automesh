from pathlib import Path

from automesh.io import load_obj, save_obj
from automesh.mesh import Mesh


def test_obj_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cube.obj"
    mesh = Mesh.cube()
    save_obj(mesh, path)

    loaded = load_obj(path)

    assert loaded.vertex_count == mesh.vertex_count
    assert loaded.face_count == mesh.face_count
