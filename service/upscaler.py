"""Upscale engines: ncnn (upscayl-bin) with a Pillow fallback.

No mandatory third-party dependency. Pillow is only used for the "pillow"
engine and for exact resizing when the requested scale differs from the
model's native scale.
"""

from __future__ import annotations

import io
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BINARY_NAMES = (
    "upscayl-bin.exe",
    "upscayl-bin",
    "realesrgan-ncnn-vulkan.exe",
    "realesrgan-ncnn-vulkan",
)

NCNN_TIMEOUT_SECONDS = 900

# ncnn prints its Vulkan devices when it starts, e.g.:
#   [0 NVIDIA GeForce RTX 4060]  queueC=2[8]  queueG=0[16]  queueT=1[1]
_DEVICE_PATTERN = re.compile(r"^\[(\d+)\s+([^\]]+?)\]\s", re.MULTILINE)
_DEVICES: list[dict] = []

# upscayl-bin.exe is a console program. When this service runs under pythonw (hidden
# autostart) there is no console to inherit, so Windows would create a new console window
# for every run. CREATE_NO_WINDOW keeps the child silent in that case.
_NO_WINDOW_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)

_SCALE_PATTERN = re.compile(r"(?<![0-9])(?:x([2-8])|([2-8])x)(?![0-9])", re.IGNORECASE)


class UpscaleError(RuntimeError):
    """Error whose message is safe to show to plugin users."""


class UpscaleCancelled(RuntimeError):
    """The caller asked to stop the run; nothing is saved."""


def _raise_if_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UpscaleCancelled("cancelled")


def _stop_process(process: "subprocess.Popen") -> None:
    """Terminate a child process and make sure it is gone before returning."""
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001 - best effort shutdown
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - already gone
            pass


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _remember_devices(output: str | None) -> None:
    """Cache the Vulkan device list ncnn prints, for the system report.

    ncnn prints the list once per worker, so the same device shows up several times;
    duplicates are collapsed by index + name, keeping the first occurrence order.
    """
    global _DEVICES
    if not output:
        return
    found = _DEVICE_PATTERN.findall(output)
    if not found:
        return
    devices: list[dict] = []
    for index, name in found:
        entry = {"index": int(index), "name": name.strip()}
        if entry not in devices:
            devices.append(entry)
    _DEVICES = devices


def gpu_devices() -> list[dict]:
    """Vulkan devices seen so far (empty until the binary has run once)."""
    return list(_DEVICES)


def _tiny_png(size: int = 8) -> bytes:
    import zlib

    rows = b"".join(b"\x00" + bytes((132, 96, 64)) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def probe_devices(binary: Path | None, models: list[dict], extra_args: list[str]) -> list[dict]:
    """Run one tiny upscale so ncnn prints its Vulkan device list."""
    if not binary or not models:
        return gpu_devices()
    entry = models[0]
    try:
        _run_ncnn(binary, _tiny_png(8), entry, entry["scale"] or 2, extra_args)
    except UpscaleError:
        pass
    return gpu_devices()


def upscayl_resource_roots() -> list[Path]:
    """Upscayl's `resources/` folder on this platform (bin/ and models/ live inside)."""
    roots: list[Path] = []
    if os.name == "nt":
        for base in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if not base:
                continue
            roots.append(Path(base) / "Upscayl" / "resources")
            roots.append(Path(base) / "Programs" / "Upscayl" / "resources")
    elif sys.platform == "darwin":
        roots.extend(
            [
                Path("/Applications/Upscayl.app/Contents/Resources"),
                Path.home() / "Applications/Upscayl.app/Contents/Resources",
            ]
        )
    else:
        roots.extend(
            [
                Path("/usr/lib/upscayl/resources"),
                Path("/usr/share/upscayl/resources"),
                Path("/opt/Upscayl/resources"),
                Path("/opt/upscayl/resources"),
                Path.home() / ".local/share/upscayl/resources",
                # Flatpak and AppImage installs
                Path("/var/lib/flatpak/app/org.upscayl.Upscayl/current/active/files/resources"),
                Path.home()
                / ".local/share/flatpak/app/org.upscayl.Upscayl/current/active/files/resources",
            ]
        )
    return roots


def find_binary(explicit: str | None, project_dir: Path) -> Path | None:
    """Locate upscayl-bin / realesrgan-ncnn-vulkan in the usual places for this OS."""
    candidates: list[Path] = []

    env = os.environ.get("PHOTON_UPSCALE_BIN")
    if explicit:
        candidates.append(Path(explicit))
    if env:
        candidates.append(Path(env))

    for name in BINARY_NAMES:
        candidates.append(project_dir / "bin" / name)
        for root in upscayl_resource_roots():
            candidates.append(root / "bin" / name)
        found = _which(name)
        if found:
            candidates.append(Path(found))

    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def bundled_upscayl_models() -> list[Path]:
    """Model folder shipped with Upscayl, when Upscayl is installed."""
    roots = [root / "models" for root in upscayl_resource_roots()]
    return [root for root in roots if root.is_dir()]


def model_dirs(project_models: Path) -> list[Path]:
    """The project's own model folder first, then any Upscayl collection."""
    dirs = [project_models] if project_models.is_dir() else []
    dirs.extend(bundled_upscayl_models())
    return dirs


def detect_scale(name: str) -> int:
    """Read the native scale from a model name (4xLSDIR, remacri-4x, ...-x2)."""
    match = _SCALE_PATTERN.search(name)
    if not match:
        return 0
    return int(match.group(1) or match.group(2))


def list_models(dirs: list[Path]) -> list[dict]:
    """An ncnn model is a .param + .bin pair; the stem is the model id."""
    seen: dict[str, dict] = {}
    for directory in dirs:
        try:
            params = sorted(directory.rglob("*.param"))
        except OSError:
            continue
        for param in params:
            if not param.with_suffix(".bin").is_file():
                continue
            name = param.stem
            if name in seen:
                continue
            seen[name] = {
                "name": name,
                "dir": str(param.parent),
                "scale": detect_scale(name),
            }
    return [seen[name] for name in sorted(seen)]


def pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except Exception:
        return False
    return True


def png_size(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG IHDR chunk without any dependency."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise UpscaleError("The upscaler did not return a valid PNG.")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def resize_png(data: bytes, width: int, height: int) -> bytes:
    """High-quality Lanczos resize (used for supersample: upscale, then scale back)."""
    if not pillow_available():
        raise UpscaleError("Rescaling requires Pillow. Run: python -m pip install pillow")
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        resized = image.resize((max(1, width), max(1, height)), resample)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        return buffer.getvalue()


def _run_ncnn(
    binary: Path,
    png_bytes: bytes,
    entry: dict,
    native_scale: int,
    extra_args: list[str],
    cancel_event=None,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="photon-upscale-") as workdir:
        work = Path(workdir)
        source = work / "input.png"
        target = work / "output.png"
        source.write_bytes(png_bytes)

        command = [
            str(binary),
            "-i",
            str(source),
            "-o",
            str(target),
            "-s",
            str(native_scale),
            "-n",
            entry["name"],
            "-m",
            entry["dir"],
            "-f",
            "png",
        ]
        command.extend(extra_args)

        stdout_path = work / "stdout.log"
        stderr_path = work / "stderr.log"
        deadline = time.monotonic() + NCNN_TIMEOUT_SECONDS
        returncode = -1
        try:
            # stdout/stderr go to files: a pipe would deadlock when the buffer fills, and the
            # process has to stay killable while it runs.
            with stdout_path.open("wb") as stdout_fh, stderr_path.open("wb") as stderr_fh:
                process = subprocess.Popen(
                    command,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    creationflags=_NO_WINDOW_FLAGS,
                )
                while True:
                    returncode = process.poll()
                    if returncode is not None:
                        break
                    if cancel_event is not None and cancel_event.is_set():
                        _stop_process(process)
                        raise UpscaleCancelled("cancelled")
                    if time.monotonic() > deadline:
                        _stop_process(process)
                        raise UpscaleError(
                            f"Upscaling took longer than {NCNN_TIMEOUT_SECONDS}s. "
                            "Try a smaller layer."
                        )
                    time.sleep(0.15)
        except FileNotFoundError as error:
            raise UpscaleError(f"The upscaler binary cannot be executed: {error}") from error

        stdout = stdout_path.read_bytes().decode("utf-8", "replace") if stdout_path.exists() else ""
        stderr = stderr_path.read_bytes().decode("utf-8", "replace") if stderr_path.exists() else ""

        # Record the Vulkan device list even when the run fails (driver problems still
        # print the devices that ncnn managed to enumerate).
        _remember_devices(stderr)
        _remember_devices(stdout)

        if returncode != 0 or not target.is_file():
            detail = (stderr or stdout or "").strip().splitlines()
            tail = " | ".join(detail[-3:]) if detail else f"exit code {returncode}"
            combined = f"{stderr}\n{stdout}".lower()
            hint = ""
            if any(
                token in combined
                for token in (
                    "vkallocatememory",
                    "out of memory",
                    "failed to allocate",
                    "vk_error_out_of_device_memory",
                    "vk_error_device_lost",
                )
            ):
                hint = (
                    ' — the GPU ran out of memory; lower the scale or set "tile" '
                    "in service/config.json"
                )
            raise UpscaleError(f"upscayl-bin failed: {tail}{hint}")

        return target.read_bytes()


def _run_pillow(png_bytes: bytes, scale: int, cancel_event=None) -> bytes:
    if not pillow_available():
        raise UpscaleError("The Pillow engine requires Pillow. Run: python -m pip install pillow")
    from PIL import Image

    _raise_if_cancelled(cancel_event)

    with Image.open(io.BytesIO(png_bytes)) as image:
        width = max(1, round(image.width * scale))
        height = max(1, round(image.height * scale))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        resized = image.resize((width, height), resample)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        return buffer.getvalue()


def choose_engine(engine: str, binary: Path | None, models: list[dict]) -> str:
    """Resolve "auto" into a concrete engine, or fail with a helpful message."""
    if engine == "ncnn":
        if not binary:
            raise UpscaleError(
                "upscayl-bin was not found. Install Upscayl or set PHOTON_UPSCALE_BIN."
            )
        if not models:
            raise UpscaleError("No .param/.bin model pairs were found in the model folders.")
        return "ncnn"
    if engine == "pillow":
        if not pillow_available():
            raise UpscaleError("The Pillow engine requires Pillow: python -m pip install pillow")
        return "pillow"
    if binary and models:
        return "ncnn"
    if pillow_available():
        return "pillow"
    raise UpscaleError(
        "No engine is ready: upscayl-bin was not found and Pillow is not installed."
    )


def upscale(
    png_bytes: bytes,
    *,
    scale: int,
    model: str,
    engine: str,
    project_dir: Path,
    models_dir: Path,
    binary: Path | None,
    extra_args: list[str],
    cancel_event=None,
) -> dict:
    """Upscale a PNG and return the resulting bytes plus metadata."""
    if not 1 <= scale <= 8:
        raise UpscaleError("Scale must be between 1 and 8.")
    if not png_bytes:
        raise UpscaleError("The image payload is empty.")
    _raise_if_cancelled(cancel_event)

    dirs = model_dirs(models_dir)
    models = list_models(dirs)
    active = choose_engine(engine, binary, models)

    started = time.perf_counter()
    note = ""
    used_model = ""
    native_scale = scale

    if active == "pillow":
        output = _run_pillow(png_bytes, scale, cancel_event)
    else:
        entry = next((item for item in models if item["name"] == model), None)
        if entry is None:
            entry = next((item for item in models if item["scale"] == scale), None) or models[0]
            if model:
                note = f"model '{model}' is unavailable; using '{entry['name']}'"
        native_scale = entry["scale"] or scale
        used_model = entry["name"]
        try:
            output = _run_ncnn(binary, png_bytes, entry, native_scale, extra_args, cancel_event)
        except UpscaleError as error:
            # ncnn needs a Vulkan device. On CPU-only machines (or a broken driver) fall
            # back to Pillow instead of failing the whole run, unless ncnn was explicit.
            if engine == "ncnn":
                raise
            if not pillow_available():
                raise UpscaleError(
                    f"{error} — and Pillow is not installed for the CPU fallback "
                    "(python -m pip install pillow)"
                ) from error
            output = _run_pillow(png_bytes, scale, cancel_event)
            active = "pillow"
            used_model = ""
            native_scale = scale
            note = f"ncnn failed ({error}); fell back to the Pillow CPU resize"

        if active == "ncnn" and scale != native_scale:
            if pillow_available():
                source_width, source_height = png_size(png_bytes)
                output = resize_png(
                    output,
                    max(1, round(source_width * scale)),
                    max(1, round(source_height * scale)),
                )
            else:
                note = (
                    f"the model upscales {native_scale}× natively; install Pillow "
                    "to resize exactly to the requested scale"
                )

    width, height = png_size(output)
    result = {
        "png": output,
        "engine": active,
        "model": used_model,
        "nativeScale": native_scale,
        "scale": scale,
        "width": width,
        "height": height,
        "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
    }
    if note:
        result["note"] = note
    return result


def describe(models_dir: Path, binary: Path | None) -> dict:
    """Snapshot of what the service can currently do."""
    dirs = model_dirs(models_dir)
    return {
        "binary": str(binary) if binary else None,
        "models": list_models(dirs),
        "modelDirs": [str(directory) for directory in dirs],
        "pillow": pillow_available(),
    }
