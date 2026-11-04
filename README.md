# Photon Upscale

Free, open-source upscaling panel for Photon Studio. Select a layer, pick a model, and get a
higher-resolution version back as a new layer in the same document, at the same position.

Upscaling runs in a small **local service on `127.0.0.1`** (the plugin webview has no native
runtime), with an **ncnn** engine when `upscayl-bin` and models are present, or a **Pillow** fallback.

## Requirements

| Item | Notes |
| --- | --- |
| Photon Studio 0.1.66+ | Plugin SDK 1 |
| Python 3.10+ | 3.15 tested; the service only needs the standard library |
| `upscayl-bin` + `.param`/`.bin` models | Optional — without them the Pillow engine is used |
| Pillow | `python -m pip install pillow` (fallback engine) |

Models go in `service/models/` (not in git because of their size).

## Screenshots

| Panel | Running | Service details |
| --- | --- | --- |
| ![Panel](docs/panel-dark.png) | ![Running](docs/panel-running.png) | ![Details](docs/panel-details.png) |

## Design system

Panel styling, components and tokens follow [`../design-system`](../design-system/README.md):
24 px controls, 7 px control radius, 12 px floating surfaces, the canonical `select.js`
dropdown and the shared header/footer contract.

## Install

```powershell
# 1. Start the service (leave the window open; Windows: tools\start-service.cmd also works)
.\tools\start-service.ps1

# 2. Photon Studio → Preferences → Plug-ins → Install plugin → select this repo's plugin/ folder

# 3. In Photon: select a raster layer → Plugins → Upscale (Ctrl+Shift+U) → Upscale Layer
```

macOS / Linux use the shell equivalents:

```bash
./tools/start-service.sh          # the .sh files are committed executable
# if your checkout lost the bit:  chmod +x tools/*.sh
```

### Run the service hidden (recommended, Windows)

```powershell
.\tools\install-autostart.ps1          # hidden now and at every sign-in
.\tools\stop-service.ps1               # stop the hidden instance
.\tools\install-autostart.ps1 -Remove  # undo everything
```

Autostart is a per-user `HKCU\...\Run` entry (no admin rights, nothing system-wide). Logs live in
`service/logs/service.log` and rotate at 2 MB. macOS/Linux: `tools/install-autostart.sh` installs a
launchd LaunchAgent / systemd `--user` unit.

## Use

1. Select a raster layer (rasterize text or vector layers first).
2. Choose scale (**2×**, **3×** or **4×**) and an engine/quality profile.
3. Press **Upscale**; progress and stage are shown, and the result lands above the source layer at
   the same position.
4. **Details** next to *Check service* shows system info, available models and the service log.

Scale is a free number field: any value from 1× upward is accepted, with no output-size cap.
Only the source layer has to fit Photon's pixel transfer (16 MP); how large the *result* may be is
decided by Photon when the pixels are written back.

## Changelog

### 1.0.28

- Header now includes the design-system activity word (`Idle · Ready`) next to the badge.

### 1.0.27

- **Scale is unrestricted.** The Scale control is a free number field (was a 1–4× dropdown), the
  plugin no longer refuses large results (`checkScaled`/`checkPadded` removed) and the service accepts
  any scale ≥ 1 (was 1–8). The "Scale too high" hint is gone; if Photon itself refuses a result, its
  own error is shown.

## Testing

```powershell
node tools/upscale-test.cjs     # 7 tests: 8192 px / 16 MP limits, fallback placement
```

## Limits

- The service must be running; the panel's **Check service** button reports its version and models.
- Intel Macs work through the same service; Apple Silicon needs the arm64 `upscayl-bin`.
- GPU out-of-memory during a run: lower the scale or set `"tile": 512` in `service/config.json`.

MIT licensed — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
