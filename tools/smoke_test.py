"""Smoke test for the Photon Upscale service.

Starts the server on a temporary port, sends a synthetic PNG through the same
multipart request the plugin uses, then checks the returned file.

    python tools/smoke_test.py
    python tools/smoke_test.py --engine ncnn --scale 4
    python tools/smoke_test.py --engine pillow --scale 2
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = ROOT / "service"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def make_png(width: int, height: int) -> bytes:
    """Encode a small RGB gradient PNG with the standard library only."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row += bytes(((x * 8) % 256, (y * 8) % 256, ((x + y) * 4) % 256))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def multipart(fields: list[dict], boundary: str) -> bytes:
    out = bytearray()
    for field in fields:
        out += b"--" + boundary.encode() + b"\r\n"
        if "bytes" in field:
            disposition = (
                f'Content-Disposition: form-data; name="{field["name"]}"; '
                f'filename="{field.get("filename", "blob.png")}"\r\n'
            )
            out += disposition.encode()
            out += b"Content-Type: image/png\r\n\r\n" + field["bytes"] + b"\r\n"
        else:
            disposition = f'Content-Disposition: form-data; name="{field["name"]}"\r\n\r\n'
            out += disposition.encode() + str(field["text"]).encode() + b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return bytes(out)


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def post_upscale(png: bytes, scale: int, model: str, engine: str) -> tuple[int, dict]:
    boundary = "----photonupscaletest123"
    body = multipart(
        [
            {"name": "image", "bytes": png, "filename": "layer.png"},
            {"name": "scale", "text": scale},
            {"name": "model", "text": model},
            {"name": "engine", "text": engine},
        ],
        boundary,
    )
    request = urllib.request.Request(
        f"{BASE}/upscale",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def post_upscale_bytes(
    png: bytes,
    scale: int,
    model: str,
    engine: str,
    target: tuple[int, int] | None = None,
) -> tuple[int, bytes, dict]:
    """Same request as the panel: response=bytes plus base64 JSON metadata header."""
    boundary = "----photonupscaletest456"
    fields = [
        {"name": "image", "bytes": png, "filename": "layer.png"},
        {"name": "scale", "text": scale},
        {"name": "model", "text": model},
        {"name": "engine", "text": engine},
        {"name": "response", "text": "bytes"},
    ]
    if target:
        fields.append({"name": "targetWidth", "text": target[0]})
        fields.append({"name": "targetHeight", "text": target[1]})
    body = multipart(fields, boundary)
    request = urllib.request.Request(
        f"{BASE}/upscale",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            meta_header = response.headers.get("x-upscale-meta") or ""
            meta = {}
            if meta_header:
                meta = json.loads(base64.b64decode(meta_header).decode("utf-8"))
            return response.status, response.read(), meta
    except urllib.error.HTTPError as error:
        return error.code, error.read(), {}


def wait_ready(process: subprocess.Popen, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("the server exited before becoming ready")
        try:
            return get_json("/health")
        except (urllib.error.URLError, ConnectionError, OSError) as error:
            last_error = error
            time.sleep(0.4)
    raise RuntimeError(f"the server did not become ready: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="auto", choices=["auto", "ncnn", "pillow"])
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--width", type=int, default=64)
    parser.add_argument("--height", type=int, default=48)
    parser.add_argument("--model", default="")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="photon-upscale-test-") as out_dir:
        command = [
            sys.executable,
            str(SERVICE_DIR / "server.py"),
            "--port",
            str(PORT),
            "--out",
            out_dir,
        ]
        process = subprocess.Popen(
            command,
            cwd=str(SERVICE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        failures: list[str] = []
        logs = ""
        try:
            health = wait_ready(process)
            print(f"health ok · python {health['python']} · {len(health['models'])} model(s)")
            print(f"binary: {health['binaryPath'] or 'not found'} · pillow: {health['pillow']}")

            models = get_json("/models")["models"]
            sample = ", ".join(item["name"] for item in models[:4])
            print(f"/models ok · e.g. {sample}")
            if not models and health["binaryPath"]:
                failures.append("/models is empty even though the ncnn binary exists")

            png = make_png(args.width, args.height)
            print(f"input {args.width}x{args.height} ({len(png)} bytes), engine={args.engine}")

            status, body = post_upscale(png, args.scale, args.model, args.engine)
            if status != 200 or not body.get("ok"):
                failures.append(f"/upscale failed: status {status} {body.get('error')}")
                print(json.dumps(body, indent=2, ensure_ascii=False))
            else:
                model_label = f"/{body['model']}" if body["model"] else ""
                print(
                    f"/upscale ok · {body['width']}x{body['height']} · "
                    f"{body['engine']}{model_label} · {body['elapsedMs']} ms"
                )
                if body.get("note"):
                    print(f"note: {body['note']}")

                output = Path(body["path"])
                if not output.is_file():
                    failures.append(f"result file is missing: {output}")
                elif output.stat().st_size != body["bytes"]:
                    failures.append("result file size does not match the response")

                effective = body.get("nativeScale") or args.scale
                expected = (args.width * effective, args.height * effective)
                if (body["width"], body["height"]) != expected:
                    failures.append(
                        f"dimensions {body['width']}x{body['height']} != expected "
                        f"{expected[0]}x{expected[1]}"
                    )

            # Second pass: the raw-bytes mode the panel uses.
            status, payload, meta = post_upscale_bytes(png, args.scale, args.model, args.engine)
            if status != 200:
                failures.append(f"/upscale (bytes mode) failed with status {status}")
            elif not payload.startswith(b"\x89PNG"):
                failures.append("/upscale (bytes mode) did not return a PNG body")
            elif not meta.get("width"):
                failures.append("/upscale (bytes mode) is missing the x-upscale-meta header")
            else:
                print(
                    f"/upscale bytes mode ok · body {len(payload)} bytes · "
                    f"meta {meta['width']}x{meta['height']} {meta['engine']}"
                )

            # Third pass: supersample, i.e. upscale then scale back to the layer size.
            status, payload, meta = post_upscale_bytes(
                png,
                args.scale,
                args.model,
                args.engine,
                target=(args.width, args.height),
            )
            if status != 200:
                failures.append(f"/upscale (rescale) failed with status {status}")
            elif (meta.get("width"), meta.get("height")) != (args.width, args.height):
                failures.append(
                    f"/upscale (rescale) returned {meta.get('width')}x{meta.get('height')}, "
                    f"expected {args.width}x{args.height}"
                )
            elif not meta.get("rescaledTo"):
                failures.append("/upscale (rescale) did not report rescaledTo")
            else:
                print(
                    f"/upscale rescale ok · {meta['rescaledFrom'][0]}x{meta['rescaledFrom'][1]}"
                    f" -> {meta['width']}x{meta['height']} · body {len(payload)} bytes"
                )
        finally:
            process.terminate()
            try:
                logs, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                logs, _ = process.communicate()

        if failures:
            print("\nFAILED:")
            for item in failures:
                print(f"  - {item}")
            if logs:
                print("\n--- server log ---")
                print(logs[-3000:])
            return 1

        print("\nALL CHECKS PASSED")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
