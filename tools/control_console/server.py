"""Static asset and Server-Sent Events server for the live console."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from dex_runtime.telemetry import TelemetryHub


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
FONT_SHA256 = {
    "fonts/ui-regular.woff2": "2337ce1eabe439c99ca918306a4d0c4ed48697ec5b128c2fb70c1df828a4eddd",
    "fonts/ui-bold.woff2": "d2ccbec1bd5769e9cfe3b6b63472d9af24fbf520539a144ead00464a543be48b",
    "fonts/ui-mono.woff2": "e981f918e72d65262246e43609e254a6a4eac48e49a6c42a9c23e0465abd22f3",
}
_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/fonts/ui-regular.woff2": ("fonts/ui-regular.woff2", "font/woff2"),
    "/assets/fonts/ui-bold.woff2": ("fonts/ui-bold.woff2", "font/woff2"),
    "/assets/fonts/ui-mono.woff2": ("fonts/ui-mono.woff2", "font/woff2"),
}


def verify_assets(assets_dir: Path = ASSETS_DIR) -> None:
    for relative in ("index.html", "app.css", "app.js", *FONT_SHA256):
        path = assets_dir / relative
        if not path.is_file():
            raise RuntimeError(f"required console asset is missing: {relative}")
    for relative, expected in FONT_SHA256.items():
        actual = hashlib.sha256((assets_dir / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"console font digest mismatch for {relative}: {actual} != {expected}"
            )


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        controller,
        hub: TelemetryHub,
        assets_dir: Path,
        camera=None,
    ) -> None:
        self.controller = controller
        self.hub = hub
        self.assets_dir = assets_dir
        self.camera = camera
        self.console_stop = threading.Event()
        super().__init__(server_address, ConsoleRequestHandler)

    def server_close(self) -> None:
        self.console_stop.set()
        super().server_close()


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer

    def log_message(self, *_args) -> None:
        pass

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "font-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, value: Mapping[str, object]) -> None:
        body = json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _asset(self, path: str) -> bool:
        route = _STATIC_ROUTES.get(path)
        if route is None:
            return False
        relative, content_type = route
        body = (self.server.assets_dir / relative).read_bytes()
        cache = (
            "public, max-age=31536000, immutable"
            if relative.startswith("fonts/")
            else "no-cache"
        )
        self._send(200, body, content_type, cache_control=cache)
        return True

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self._security_headers()
        self.end_headers()

        revision = -1
        next_emit = 0.0
        last_write = 0.0
        try:
            while not self.server.console_stop.is_set():
                current = self.server.hub.wait_for_revision(revision, 0.05)
                now = time.monotonic()
                if current > revision and now < next_emit:
                    self.server.console_stop.wait(min(0.05, next_emit - now))
                    continue
                if current > revision:
                    snapshot = self.server.hub.snapshot()
                    revision = int(snapshot["revision"])
                    encoded = json.dumps(
                        snapshot,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    self.wfile.write(
                        f"id: {revision}\nevent: snapshot\ndata: ".encode("ascii")
                        + encoded
                        + b"\n\n"
                    )
                    self.wfile.flush()
                    last_write = now
                    next_emit = now + 0.05
                elif now - last_write >= 1.0:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_write = now
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _mjpeg(self, kind: str) -> None:
        camera = self.server.camera
        if camera is None:
            self._send(503, b"camera disabled", "text/plain; charset=utf-8")
            return
        boundary = b"dex-camera-frame"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            f"multipart/x-mixed-replace; boundary={boundary.decode('ascii')}",
        )
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        sequence = -1
        try:
            while not self.server.console_stop.is_set():
                frame = camera.wait_for_frame(kind, sequence, 1.0)
                if frame is None:
                    continue
                sequence, jpeg = frame
                self.wfile.write(b"--" + boundary + b"\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii"))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if not self._asset(path):
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if self._asset(path):
            return
        if path == "/api/snapshot":
            self._json(200, self.server.hub.snapshot())
            return
        if path == "/api/live":
            self._sse()
            return
        if path == "/api/camera/rgb.mjpg":
            self._mjpeg("rgb")
            return
        if path == "/api/camera/depth.mjpg":
            self._mjpeg("depth")
            return
        if path == "/api/status":
            self._json(200, self.server.controller.snapshot())
            return
        if path == "/api/vr":
            snapshot = self.server.hub.snapshot()
            openxr = snapshot["sources"].get("openxr")
            if openxr is None:
                self._json(200, {"connected": False, "nodes": []})
            else:
                payload = dict(openxr["payload"])
                payload["connected"] = openxr["health"] == "healthy"
                payload["age_ms"] = openxr["age_ms"]
                self._json(200, payload)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"ok": False, "message": "Invalid content length."})
            return
        if content_length > 4096:
            self._json(413, {"ok": False, "message": "Request body is too large."})
            return
        if content_length:
            self.rfile.read(content_length)

        if path == "/api/confirm":
            result = self.server.controller.do_confirm()
        elif path == "/api/switch":
            result = self.server.controller.do_switch()
        elif path == "/api/stop":
            result = self.server.controller.do_stop()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._json(200, result)


def make_console_server(
    host: str,
    port: int,
    *,
    controller,
    hub: TelemetryHub,
    assets_dir: Path = ASSETS_DIR,
    camera=None,
) -> ConsoleHTTPServer:
    verify_assets(assets_dir)
    return ConsoleHTTPServer(
        (host, port),
        controller=controller,
        hub=hub,
        assets_dir=assets_dir,
        camera=camera,
    )
