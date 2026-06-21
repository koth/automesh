from pathlib import Path

from automesh.io import load_obj, mesh_from_obj_string, mesh_to_obj_string, save_obj
from automesh.mesh import Mesh


def test_obj_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cube.obj"
    mesh = Mesh.cube()
    save_obj(mesh, path)

    loaded = load_obj(path)

    assert loaded.vertex_count == mesh.vertex_count
    assert loaded.face_count == mesh.face_count


def test_obj_string_roundtrip_matches_file() -> None:
    """mesh_to_obj_string must match save_obj byte-for-byte (sans newline style)."""
    mesh = Mesh.cube()
    text = mesh_to_obj_string(mesh)
    loaded = mesh_from_obj_string(text)
    assert loaded.vertex_count == mesh.vertex_count
    assert loaded.face_count == mesh.face_count

    # The on-disk and in-memory serializations must be identical so the render
    # service parses them the same way regardless of transport.
    import tempfile
    from pathlib import Path as P

    with tempfile.NamedTemporaryFile("w", suffix=".obj", delete=False) as f:
        save_obj(mesh, f.name)
        disk = P(f.name).read_text()
    assert disk == text
    assert disk.endswith("\n")
