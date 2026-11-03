# Third-party notices

This repository contains **only original code**:

| Path | Contents | License |
| --- | --- | --- |
| `plugin/` | Photon plugin: manifest, HTML, CSS, JS | MIT (see [LICENSE](LICENSE)) |
| `service/` | Python HTTP service + engine wrappers | MIT |
| `tools/` | Launcher and autostart scripts | MIT |
| `docs/` | Screenshots of this plugin | MIT |

**No model weights and no third-party binaries are distributed with this project.** The
`.gitignore` excludes `service/models/*` and the service never ships `upscayl-bin`.

## Upscayl

The service can call a locally installed **Upscayl** binary that the user installed:

- Upscayl is published under **AGPL-3.0** (verified in the installed package metadata for
  version 2.15.0).
- This project does **not** include, modify, or redistribute Upscayl or its binary. It starts
  the user's own copy as a separate process on the same machine, which is ordinary use of an
  installed tool rather than linking against it.
- If you fork this project and bundle the binary or the Upscayl application, **you** take on
  the AGPL obligations (source availability, notices) and you must verify that your
  distribution is allowed.

`upscayl-bin` itself is a build of the ncnn/Vulkan super-resolution CLI used by Upscayl. Check
the upstream project you build or copy from before redistributing it.

## Model weights

Model licenses are **set by each model author** and they are not uniform:

- The official Real-ESRGAN models (`RealESRGAN_General_x4_v3`, `realesr-animevideov3-x*`, …)
  come from the Real-ESRGAN project; keep that project's terms in mind.
- Community models that ship with Upscayl (for example `4x-UltraSharp`, `remacri-4x`,
  `4x_NMKD-*`, `4xHFA2k`, `4xLSDIR*`, `4xNomos8kSC`, `uniscale_restore`,
  `upscayl-standard-4x`, …) each have their own license, and several are limited to
  **non-commercial** use or require author credit.
- The Upscayl installation inspected for this project contains **no license or attribution
  files inside its `models/` folder**, so the terms cannot be read from the download alone.

Consequences:

1. This project never redistributes model files, so it does not grant or imply any model
   license.
2. Whoever runs an upscale is responsible for having the right to use the chosen model, in
   particular for commercial output.
3. When in doubt, use a model you can trace to an explicit license, or keep the model only for
   private experiments.

## Images

Upscaled output is produced from the user's own document. Whether that output may be published
or sold depends on the source image rights **and** the model license — not on this project.

---

Nothing here is legal advice. If you plan to publish or sell this plugin together with
third-party models or binaries, review the AGPL and the individual model terms first.
