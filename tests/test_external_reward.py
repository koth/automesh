import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from automesh.mesh import Mesh
from automesh.qem import QEMSimplifier
from automesh.rewards import (
    ExternalCommandReward,
    HttpRenderReward,
    RewardContext,
    render_mesh_image,
)


def test_external_command_reward_reads_float() -> None:
    mesh = Mesh.cube()
    simplifier = QEMSimplifier()
    candidate = simplifier.candidates(mesh, top_k=1)[0]
    current = simplifier.collapse(mesh, candidate).mesh
    reward = ExternalCommandReward(
        command=[sys.executable, "-c", "print(0.25)"],
        interval=1,
    )

    value = reward(
        RewardContext(
            original=mesh,
            previous=mesh,
            current=current,
            candidate=candidate,
            step_index=1,
            target_faces=4,
            done=False,
        )
    )

    assert value == 0.25


def _make_context() -> tuple[RewardContext, Mesh, Mesh]:
    original = Mesh.cube()
    simplifier = QEMSimplifier()
    candidate = simplifier.candidates(original, top_k=1)[0]
    current = simplifier.collapse(original, candidate).mesh
    return (
        RewardContext(
            original=original,
            previous=original,
            current=current,
            candidate=candidate,
            step_index=100,
            target_faces=4,
            done=False,
        ),
        original,
        current,
    )


def test_http_render_reward_sends_obj_content_and_reads_reward() -> None:
    """HttpRenderReward must POST the OBJ text in the body and parse {"reward": float}."""
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            received["body"] = json.loads(raw.decode("utf-8"))
            resp = json.dumps({"reward": 0.42}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx, original, current = _make_context()
        reward = HttpRenderReward(
            endpoint=f"http://127.0.0.1:{port}/reward",
            interval=1,
            weight=2.0,
        )
        value = reward(ctx)
    finally:
        server.shutdown()

    # The contract: OBJ content travels in the body, not file paths.
    assert "original_obj" in received["body"]
    assert "current_obj" in received["body"]
    assert received["body"]["original_obj"].startswith("v ")
    assert received["body"]["current_obj"].startswith("v ")
    assert received["body"]["faces"] == current.face_count
    assert received["body"]["step"] == 100
    assert "original" not in received["body"]  # no path field anymore
    assert value == 2.0 * 0.42


def _start_canned_server(status: int, content_type: str, body: bytes, capture: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", "0"))
            capture["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_render_mesh_image_sends_obj_and_returns_png_bytes() -> None:
    """render_mesh_image must POST {"obj": ...} and return the raw PNG body."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    capture: dict = {}
    server, port = _start_canned_server(200, "image/png", png, capture)
    try:
        out = render_mesh_image(Mesh.cube(), endpoint=f"http://127.0.0.1:{port}/render")
    finally:
        server.shutdown()

    assert capture["body"]["obj"].startswith("v ")
    assert capture["body"]["width"] == 1024
    assert capture["body"]["height"] == 1024
    assert out == png


def test_render_mesh_image_raises_on_json_error_body() -> None:
    """A 200 + application/json {"error": ...} must raise RuntimeError."""
    capture: dict = {}
    server, port = _start_canned_server(
        200, "application/json", json.dumps({"error": "bad mesh"}).encode(), capture
    )
    try:
        raised = False
        try:
            render_mesh_image(Mesh.cube(), endpoint=f"http://127.0.0.1:{port}/render")
        except RuntimeError as exc:
            raised = True
            assert "bad mesh" in str(exc)
    finally:
        server.shutdown()
    assert raised


def test_render_mesh_image_cli_writes_png(tmp_path) -> None:
    """End-to-end CLI: `automesh render-image in.obj out.png` writes the bytes."""
    from automesh.io import save_obj

    png = b"\x89PNG\r\n\x1a\n" + b"\x01" * 8
    capture: dict = {}
    server, port = _start_canned_server(200, "image/png", png, capture)
    try:
        obj_path = tmp_path / "in.obj"
        out_path = tmp_path / "out.png"
        save_obj(Mesh.cube(), obj_path)
        import subprocess

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "automesh.cli",
                "render-image",
                str(obj_path),
                str(out_path),
                "--endpoint",
                f"http://127.0.0.1:{port}/render",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        server.shutdown()

    assert out_path.read_bytes() == png
    assert "saved" in completed.stdout
