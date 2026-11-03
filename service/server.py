"""Local HTTP service for the Photon Upscale plugin.

It only listens on 127.0.0.1. The plugin sends the selected layer as a PNG to
POST /upscale; the service runs a model (ncnn or Pillow), saves the result as a
PNG file and returns it either as JSON metadata or as a raw PNG body with the
metadata in a `x-upscale-meta` header, so the plugin can write the pixels into a
new layer with `documents.writePixels`.

Run:
    python server.py                       # 127.0.0.1:8765
    python server.py --port 9000 --engine pillow
    python server.py --bin "C:\\Program Files\\Upscayl\\resources\\bin\\upscayl-bin.exe"
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sysinfo
import upscaler


def _discover_version() -> str:
    """One version for the whole release: the plugin manifest is the source of truth.

    The service sits next to plugin/, so it reports the same number the Photon plugin
    list shows and the two can never drift apart.
    """
    manifest = Path(__file__).resolve().parent.parent / "plugin" / "photon.plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except (OSError, ValueError):
        pass
    return "1.0.0"


SERVICE_VERSION = _discover_version()
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "models": "models",
    "out": "out",
    "binary": "",
    "engine": "auto",
    "gpu": "",
    "tile": 0,
    "extraArgs": [],
}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(service_dir: Path) -> dict:
    """config.json is optional; CLI flags override it."""
    config = dict(DEFAULT_CONFIG)
    path = service_dir / "config.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update({key: value for key, value in loaded.items() if key in DEFAULT_CONFIG})
        except (OSError, ValueError) as error:
            log(f"config.json could not be read ({error}); using defaults")
    return config


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, dict]:
    """Minimal multipart/form-data parser (the cgi module was removed in 3.13)."""
    parts: dict[str, dict] = {}
    delimiter = b"--" + boundary
    for raw in body.split(delimiter):
        if not raw or raw.startswith(b"--"):
            continue
        chunk = raw[2:] if raw.startswith(b"\r\n") else raw
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("utf-8", "replace")
        name_match = re.search(r'name="([^"]*)"', headers)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', headers)
        parts[name_match.group(1)] = {
            "filename": filename_match.group(1) if filename_match else None,
            "bytes": data,
        }
    return parts


class UpscaleService:
    def __init__(self, config: dict, service_dir: Path) -> None:
        self.service_dir = service_dir
        self.host = str(config["host"])
        self.port = int(config["port"])
        self.engine = str(config["engine"] or "auto")
        self.extra_args = [str(item) for item in config.get("extraArgs") or []]
        # upscayl-bin talks to the GPU through Vulkan, so `-g` picks the device by index
        # (NVIDIA, AMD and Intel are all equal here) and `-t` caps the tile size.
        self.gpu = str(config.get("gpu") or "").strip()
        self.tile = int(config.get("tile") or 0)
        if self.gpu:
            self.extra_args += ["-g", self.gpu]
        if self.tile > 0:
            self.extra_args += ["-t", str(self.tile)]
        self.models_dir = self._resolve(str(config["models"]))
        self.out_dir = self._resolve(str(config["out"]))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.binary = upscaler.find_binary(str(config.get("binary") or "") or None, self.service_dir)
        self.lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.progress = {"value": 0.0, "label": "idle", "estimateMs": 0}

        # Cancellation: one token per run, replaced by run_upscale() each time.
        self.cancel_event = threading.Event()
        self.running = False
        self.durations: dict[str, float] = {}

    # ---------- progress ----------

    def begin_progress(self, estimate_ms: float = 0, label: str = "starting") -> None:
        with self.progress_lock:
            self.progress = {
                "value": 0.0,
                "label": label,
                "estimateMs": int(estimate_ms),
                "startedAt": time.time(),
            }

    def set_estimate(self, estimate_ms: float) -> None:
        with self.progress_lock:
            self.progress["estimateMs"] = int(estimate_ms)

    def set_progress(self, value: float, label: str) -> None:
        """Monotonic: the bar never goes backwards within a request."""
        with self.progress_lock:
            self.progress["value"] = max(self.progress["value"], min(1.0, value))
            self.progress["label"] = label

    def progress_state(self) -> dict:
        with self.progress_lock:
            state = dict(self.progress)
        started = state.get("startedAt")
        state["elapsedMs"] = int((time.time() - started) * 1000) if started else 0
        return state

    def remember_duration(self, key: str, elapsed_ms: float) -> None:
        with self.progress_lock:
            self.durations[key] = elapsed_ms

    def duration_estimate(self, key: str, output_megapixels: float) -> float:
        with self.progress_lock:
            known = self.durations.get(key)
        if known:
            return known
        # First run for this model/scale/size: ncnn cost scales with the OUTPUT pixels.
        return 300 + output_megapixels * 4000

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.service_dir / path).resolve()

    def health(self) -> dict:
        info = upscaler.describe(self.models_dir, self.binary)
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "engine": self.engine,
            "binaryPath": info["binary"],
            "pillow": info["pillow"],
            "models": info["models"],
            "modelDirs": info["modelDirs"],
            "outDir": str(self.out_dir),
            "python": sys.version.split()[0],
            # Hardware routing: ncnn uses Vulkan (vendor-agnostic); Pillow is the CPU path.
            "gpu": self.gpu or "default",
            "tile": self.tile,
            "cpuFallback": info["pillow"],
        }

    def system_report(self, probe: bool = False) -> dict:
        """OS/CPU/RAM plus the graphics backend that will actually be used."""
        models = upscaler.list_models(upscaler.model_dirs(self.models_dir))
        devices = upscaler.gpu_devices()
        if probe and not devices:
            devices = upscaler.probe_devices(self.binary, models, self.extra_args)
        pillow = upscaler.pillow_available()
        if self.binary:
            backend = "ncnn (Vulkan)"
        elif pillow:
            backend = "pillow (CPU)"
        else:
            backend = "none"

        report = sysinfo.report()
        report.update(
            {
                "ok": True,
                "service": {
                    "version": SERVICE_VERSION,
                    "engine": self.engine,
                    "gpu": self.gpu or "default",
                    "tile": self.tile,
                    "binary": str(self.binary) if self.binary else None,
                    "pillow": pillow,
                    "models": len(models),
                },
                "devices": devices,
                "backend": backend,
                "cpuFallback": pillow,
            }
        )
        return report

    def save(self, png_bytes: bytes, source_name: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", source_name or "layer").strip("-")[:48] or "layer"
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}-{uuid.uuid4().hex[:8]}.png"
        path = self.out_dir / name
        path.write_bytes(png_bytes)
        return path

    def request_cancel(self) -> bool:
        """Ask the in-flight upscale to stop. True when a run was actually active."""
        active = self.running
        self.cancel_event.set()
        if active:
            self.set_progress(self.progress_state().get("value", 0.0), "cancelling")
        return active

    def run_upscale(
        self,
        png_bytes: bytes,
        scale: int,
        model: str,
        engine: str,
        target_width: int | None = None,
        target_height: int | None = None,
    ) -> tuple[dict, bytes]:
        """Run one upscale with a fresh cancel token, clearing the running flag afterwards."""
        self.cancel_event = threading.Event()
        self.running = True
        try:
            return self._run_upscale(
                png_bytes, scale, model, engine, target_width, target_height
            )
        finally:
            self.running = False

    def _run_upscale(
        self,
        png_bytes: bytes,
        scale: int,
        model: str,
        engine: str,
        target_width: int | None = None,
        target_height: int | None = None,
    ) -> tuple[dict, bytes]:
        """Upscale, optionally scale back to a target size, persist, and return (meta, png)."""
        source_width, source_height = upscaler.png_size(png_bytes)
        megapixels = (source_width * source_height) / 1_000_000
        estimate_key = f"{model or engine}:{scale}:{round(megapixels, 2)}"
        estimate = self.duration_estimate(estimate_key, megapixels * scale * scale)

        self.set_estimate(estimate)
        self.set_progress(0.05, "preparing layer")

        # upscayl-bin reports nothing while it runs, so a watcher advances the bar from the
        # measured duration of the previous run for the same model and size. The curve is
        # asymptotic: it keeps creeping towards the cap instead of stalling on it.
        stop_watch = threading.Event()

        def watch() -> None:
            started = time.perf_counter()
            while not stop_watch.wait(0.25):
                elapsed_ms = (time.perf_counter() - started) * 1000
                fraction = 0.15 + 0.75 * (1 - math.exp(-elapsed_ms / max(1.0, estimate)))
                self.set_progress(fraction, "upscaling with model")

        watcher = threading.Thread(target=watch, name="progress-watch", daemon=True)
        watcher.start()
        model_started = time.perf_counter()
        try:
            with self.lock:
                result = upscaler.upscale(
                    png_bytes,
                    scale=scale,
                    model=model,
                    engine=engine or self.engine,
                    project_dir=self.service_dir,
                    models_dir=self.models_dir,
                    binary=self.binary,
                    extra_args=self.extra_args,
                    cancel_event=self.cancel_event,
                )
        finally:
            stop_watch.set()
            watcher.join(timeout=1)
        self.remember_duration(estimate_key, (time.perf_counter() - model_started) * 1000)
        self.set_progress(0.9, "model finished")

        png = result.pop("png")

        # Supersample workflow: upscale for detail, then scale back to the size of the
        # layer that was exported, so the result occupies exactly the same area.
        if target_width and target_height:
            if (target_width, target_height) != (result["width"], result["height"]):
                self.set_progress(0.93, "rescaling to layer size")
                if upscaler.pillow_available():
                    png = upscaler.resize_png(png, target_width, target_height)
                    width, height = upscaler.png_size(png)
                    result.update(
                        {
                            "width": width,
                            "height": height,
                            "rescaledFrom": [result["width"], result["height"]],
                            "rescaledTo": [width, height],
                        }
                    )
                else:
                    result["note"] = (
                        "install Pillow (python -m pip install pillow) to rescale the result "
                        "back to the layer size"
                    )

        self.set_progress(0.97, "saving result")
        path = self.save(png, model or "upscale")
        result.update(
            {
                "ok": True,
                "path": str(path),
                "bytes": len(png),
                "inputBytes": len(png_bytes),
            }
        )
        self.set_progress(1.0, "done")
        return result, png


class Handler(BaseHTTPRequestHandler):
    service: UpscaleService
    server_version = f"PhotonUpscale/{SERVICE_VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - name defined by http.server
        log(f"{self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("Upload exceeds 64 MB.")
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802 - name defined by http.server
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/"):
            self._send_json(200, self.service.health())
            return
        if path == "/models":
            self._send_json(200, {"ok": True, "models": self.service.health()["models"]})
            return
        if path == "/progress":
            self._send_json(200, {"ok": True, **self.service.progress_state()})
            return
        if path == "/system":
            # ?probe=1 runs a tiny upscale so ncnn enumerates its Vulkan devices once.
            self._send_json(200, self.service.system_report(probe="probe=1" in self.path))
            return
        self._send_json(404, {"ok": False, "error": f"Unknown endpoint: {path}"})

    def do_POST(self) -> None:  # noqa: N802 - name defined by http.server
        path = self.path.split("?", 1)[0]
        if path == "/cancel":
            active = self.service.request_cancel()
            log(f"cancel requested (running={active})")
            self._send_json(200, {"ok": True, "cancelled": active})
            return
        if path != "/upscale":
            self._send_json(404, {"ok": False, "error": f"Unknown endpoint: {path}"})
            return
        try:
            body = self._read_body()
            if not body:
                raise ValueError("Empty body; send the layer PNG in the 'image' field.")

            content_type = self.headers.get("Content-Type") or ""
            scale = 2
            model = ""
            engine = self.service.engine
            png_bytes = body
            source_name = "layer"
            response_mode = "json"
            target_width = None
            target_height = None

            boundary_match = re.search(r"boundary=([^\s;]+)", content_type, re.IGNORECASE)
            if content_type.lower().startswith("multipart/") and boundary_match:
                boundary = boundary_match.group(1).strip('"').encode("utf-8")
                parts = parse_multipart(body, boundary)
                image_part = parts.get("image")
                if not image_part:
                    raise ValueError("The 'image' field is missing from the multipart body.")
                png_bytes = image_part["bytes"]
                source_name = image_part.get("filename") or "layer"
                if parts.get("scale") and parts["scale"]["bytes"]:
                    scale = int(parts["scale"]["bytes"].decode("utf-8", "replace") or 2)
                if parts.get("model"):
                    model = parts["model"]["bytes"].decode("utf-8", "replace")
                if parts.get("engine") and parts["engine"]["bytes"]:
                    engine = parts["engine"]["bytes"].decode("utf-8", "replace")
                if parts.get("response") and parts["response"]["bytes"]:
                    response_mode = parts["response"]["bytes"].decode("utf-8", "replace").strip()
                if parts.get("targetWidth") and parts["targetWidth"]["bytes"]:
                    target_width = int(parts["targetWidth"]["bytes"].decode("utf-8", "replace"))
                if parts.get("targetHeight") and parts["targetHeight"]["bytes"]:
                    target_height = int(parts["targetHeight"]["bytes"].decode("utf-8", "replace"))
            else:
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                params = dict(item.split("=", 1) for item in query.split("&") if "=" in item)
                if params.get("scale"):
                    scale = int(params["scale"])
                model = params.get("model", model)
                engine = params.get("engine", engine)
                response_mode = params.get("response", response_mode)
                if params.get("targetWidth"):
                    target_width = int(params["targetWidth"])
                if params.get("targetHeight"):
                    target_height = int(params["targetHeight"])

            if not png_bytes:
                raise ValueError("The image payload is empty.")
            if len(png_bytes) > MAX_UPLOAD_BYTES:
                raise ValueError("The image exceeds 64 MB.")
            if not png_bytes[:8].startswith(b"\x89PNG"):
                raise ValueError("Input must be PNG (as produced by Photon images.encode).")

            # Reset the reported progress so a poll cannot see the previous run's value.
            self.service.begin_progress(0, "receiving layer")
            log(f"upscale scale={scale} model={model or '-'} engine={engine} input={len(png_bytes)}B")
            result, png = self.service.run_upscale(
                png_bytes,
                scale,
                model,
                engine,
                target_width,
                target_height,
            )
            log(
                f"done {result['width']}x{result['height']} "
                f"({result['engine']}{'/' + result['model'] if result['model'] else ''}) "
                f"-> {result['path']}"
            )

            if response_mode == "bytes":
                # The plugin needs the pixels in memory (documents.applyImage takes an
                # Image, not a path), so metadata travels in a base64 JSON header.
                meta = base64.b64encode(json.dumps(result, ensure_ascii=False).encode("utf-8")).decode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("x-upscale-meta", meta)
                self.end_headers()
                self.wfile.write(png)
            else:
                self._send_json(200, result)
        except upscaler.UpscaleCancelled:
            self.service.set_progress(0.0, "cancelled")
            log("upscale cancelled")
            self._send_json(409, {"ok": False, "error": "cancelled"})
        except upscaler.UpscaleError as error:
            log(f"upscale failed: {error}")
            self._send_json(500, {"ok": False, "error": str(error)})
        except (ValueError, OSError) as error:
            log(f"invalid request: {error}")
            self._send_json(400, {"ok": False, "error": str(error)})
        except Exception as error:  # noqa: BLE001 - always answer with JSON
            log(f"unexpected error: {error!r}")
            self._send_json(500, {"ok": False, "error": f"Internal error: {error}"})


def redirect_output(log_path: Path) -> None:
    """pythonw.exe has no console: sys.stdout is None there, so print() would fail."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.is_file() and log_path.stat().st_size > 2 * 1024 * 1024:
            backup = log_path.with_suffix(".1.log")
            backup.unlink(missing_ok=True)
            log_path.rename(backup)
    except OSError:
        pass
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def port_is_taken(host: str, port: int, timeout: float = 0.4) -> bool:
    """Windows lets SO_REUSEADDR steal a listening port, so probe before binding."""
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((target, port)) == 0


def main(argv: list[str] | None = None) -> int:
    service_dir = Path(__file__).resolve().parent
    config = load_config(service_dir)

    parser = argparse.ArgumentParser(description="Local upscale service for Photon Studio")
    parser.add_argument("--host", default=config["host"], help="bind address (loopback only by default)")
    parser.add_argument("--port", type=int, default=config["port"])
    parser.add_argument("--models", default=config["models"], help="folder with .param/.bin models")
    parser.add_argument("--out", default=config["out"], help="folder for the resulting PNG files")
    parser.add_argument("--bin", default=config["binary"], help="path to upscayl-bin.exe")
    parser.add_argument(
        "--gpu",
        default=str(config.get("gpu") or ""),
        help="Vulkan device index for upscayl-bin (-g); empty = let it choose",
    )
    parser.add_argument(
        "--tile",
        type=int,
        default=int(config.get("tile") or 0),
        help="maximum tile size for upscayl-bin (-t); 0 = automatic",
    )
    parser.add_argument(
        "--log",
        default="",
        help="append stdout/stderr to this file (default: logs/service.log when there is no console)",
    )
    parser.add_argument(
        "--engine",
        default=config["engine"],
        choices=["auto", "ncnn", "pillow"],
        help="auto uses ncnn when available, otherwise Pillow",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=None,
        help="extra argument forwarded to upscayl-bin (repeatable)",
    )
    args = parser.parse_args(argv)

    # Started hidden (pythonw / autostart): there is no console, so log to a file.
    hidden = bool(args.log) or sys.stdout is None
    if hidden:
        redirect_output(Path(args.log) if args.log else service_dir / "logs" / "service.log")

    merged = {
        "host": args.host,
        "port": args.port,
        "models": args.models,
        "out": args.out,
        "binary": args.bin,
        "engine": args.engine,
        "gpu": args.gpu,
        "tile": args.tile,
        "extraArgs": args.extra_arg if args.extra_arg is not None else config["extraArgs"],
    }

    service = UpscaleService(merged, service_dir)
    handler = type("BoundHandler", (Handler,), {"service": service})

    if port_is_taken(service.host, service.port):
        # Starting twice (autostart plus a manual run) must not hijack the live service.
        log(f"port {service.port} already answers; the service is already running")
        return 0

    try:
        httpd = ThreadingHTTPServer((service.host, service.port), handler)
    except OSError as error:
        log(f"cannot listen on {service.host}:{service.port} ({error})")
        return 0
    httpd.daemon_threads = True

    health = service.health()
    log(f"Photon Upscale service {SERVICE_VERSION} (Python {health['python']})")
    log(f"listening on http://{service.host}:{service.port}")
    log(f"ncnn binary : {health['binaryPath'] or 'not found'}")
    log(f"pillow      : {'ready' if health['pillow'] else 'not installed'}")
    log(f"models      : {len(health['models'])} from {', '.join(health['modelDirs']) or 'none'}")
    log(f"output      : {health['outDir']}")
    log("stopped with tools\\stop-service.ps1" if hidden else "Press Ctrl+C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
