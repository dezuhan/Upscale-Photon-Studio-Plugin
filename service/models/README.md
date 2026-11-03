# Models

Drop your own upscale models here. The service scans this folder **recursively**
when it starts (and on every `/health` and `/models` request), so new files show up
in the panel after you click **Check service**.

> **Licensing.** Model weights are not covered by this project's MIT license. Every model
> author sets their own terms, several popular community models are non-commercial or require
> credit, and Upscayl's `models/` folder ships no license files. This folder is git-ignored, so
> nothing you put here is redistributed with the repository — but **you** are responsible for
> having the right to use each model, especially for commercial output. See
> [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md).

## Required format

An ncnn model is a **pair of files with the same base name**:

```
<name>.param     (network description, text)
<name>.bin       (weights, binary)
```

Both files are required. If the `.bin` is missing, the model is ignored.

## Where to get them

- **Upscayl** → its "Custom models" / `custom-models-main` collection, or the
  `models` folder inside your Upscayl installation. Those are already ncnn
  `.param` + `.bin` pairs and work as-is.
- Any Real-ESRGAN / ncnn model converted for `realesrgan-ncnn-vulkan`.

Not supported: `.pth`, `.ckpt`, `.onnx`, `.safetensors`. Convert them to ncnn first.

## Layout examples

Flat:

```
models/
├── 4xLSDIR.param
├── 4xLSDIR.bin
├── my-custom-4x.param
└── my-custom-4x.bin
```

Grouped in sub-folders (allowed, scanned recursively):

```
models/
└── custom/
    ├── portrait-4x.param
    └── portrait-4x.bin
```

## Naming and scale

The file name is what the panel shows, and it is how the service knows the model's
**native scale**:

| Name | Detected scale |
| --- | --- |
| `4xLSDIR` | 4× |
| `remacri-4x` | 4× |
| `realesr-animevideov3-x2` | 2× |
| `my-model` | unknown → uses the requested scale |

Keep the scale in the name whenever possible. If the model's native scale differs
from the scale you pick in the panel, the service runs the model at its native scale
and then resizes the result to the requested size (the resize needs Pillow:
`python -m pip install pillow`). Without Pillow you get the native-scale output and
the panel prints a note about it.

## Load order

This folder is searched first, then the models bundled with Upscayl:

1. `service/models/` (this folder)
2. `%ProgramFiles%\Upscayl\resources\models`
3. `%LOCALAPPDATA%\Programs\Upscayl\resources\models`

If two folders contain a model with the same name, the first one found wins.

## Verify without opening Photon

```powershell
python tools\smoke_test.py
```

The first lines report the model count and a few model names, for example:

```
health ok · python 3.15.2 · 21 model(s)
/models ok · e.g. 4xHFA2k, 4xLSDIR, my-custom-4x
```
