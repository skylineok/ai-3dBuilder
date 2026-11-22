---
name: ai-3d-builder
description: Turn a photo into a printable 3D file (solid figurine or lithophane relief) and open it in Bambu Studio. Use for 3D打印, 照片转3D, 浮雕, 拓竹, or any 3D print from an image. Do not use for CAD modeling.
---

# ai-3d-builder

Photo in, printable model out. Two modes with one decision rule, and a checked pipeline in between.

## Setup

```bash
git clone <repo> && cd ai-3d-builder
./scripts/install.sh            # venv + deps + .env + offline self-test
WITH_REMBG=1 ./scripts/install.sh   # optional: + background removal (~200 MB)
```

`install.sh` runs a lithophane build at the end, so a green run means the geometry stack works. Solid mode requires a key in `.env`:

```
TENCENT_API_KEY=sk-...
```

Keys are only read from the environment or `.env`; never hardcode one.

## Pick the mode first

| Image | Mode | Why |
| --- | --- | --- |
| Person, portrait, photo, artwork, flat graphic | `lithophane` | Facial detail lives in luminance, not depth |
| Toy, object, statue, product, lone animal | `solid` (default) | Needs a real 3D body |

Ask: does the user care that it *looks like the person/picture*, or that it *has the object's shape*? The first is lithophane, the second is solid.

Never run solid on a portrait, and never try to build a relief from a depth map — monocular depth gives you a silhouette, so the face turns into a blob. Luminance is the signal that carries faces.

## Solid mode

```bash
./scripts/build.sh photo.jpg --height 70 [--model hy-3d-3.1] [--supports]
```

Runs cloud reconstruction, then repairs, orients, scales to the target height, optionally shells, and exports. Takes 2–4 minutes and one cloud call.

Useful flags passed straight to `cli.py`:

| Flag | Effect |
| --- | --- |
| `--hollow 1.4` | Shell with a wall thickness, for vases and light parts |
| `--wall-top 1.2` | Thicker at the bottom, thinner at the top |
| `--open-top 4` | Cut an opening, turning a sealed vase into a vessel |
| `--drain 3` | Vertical drain hole through the bottom wall |
| `--base 1.2` | Base plate, raises bed contact |
| `--smooth 3` | Laplacian smoothing, 5+ erases detail |

Re-running post-processing is cheap: `--mesh outputs/<name>/model.raw.glb` skips the cloud call entirely.

Accept before delivering: `watertight` is true, `size.z` equals the target height, `print.warnings` is empty. If bed contact is under 3%, add a base plate; if the thinnest feature is under 0.8 mm, scale up.

## Lithophane mode

```bash
./scripts/build.sh portrait.png --mode lithophane --width 120
```

About 10 seconds, no key, no cloud. Thickness maps to brightness (dark = thick), inside a raised frame.

Tell the user these print settings — they are not optional:

- Print it standing up, panel perpendicular to the bed, so the thickness axis stays horizontal
- 100% infill, white or light PLA, 0.16–0.2 mm layers
- 5–10 mm brim against tipping, no supports
- View from the textured side toward the light; the back reads mirrored

Reject if `watertight` is false or the thinnest point drops below 0.6 mm.

## An existing mesh

```bash
.venv/bin/python cli.py --mesh broken.stl --height 80 --base 1.2 -o outputs/fixed
```

Same repair chain without reconstruction: weld vertices, drop loose shells, fill holes, fall back to voxels when a hole is too large to patch, then orient, size, optionally shell, export. Seconds, not minutes.

Works on a model that will not slice, has flipped normals, or has no sensible scale. The voxel fallback keeps it watertight but smooths detail — read `repair.notes` in the report to see which path ran.

## Hand off to the slicer

```bash
open -a BambuStudio outputs/<name>/model.3mf
```

Prefer 3MF (carries millimetre units). Otherwise try `OrcaSlicer`, `PrusaSlicer`, `Cura`; if none is installed, hand over the file path. A generic 3MF triggers "not from Bambu Lab, only geometry and colour" — that is expected, not an error. Confirm the model actually landed on the plate; do not trust the exit code alone.

Deliver: dimensions, face count, bed contact, overhang share, whether supports are needed.

## Limits worth stating out loud

- Single-image reconstruction invents whatever the camera could not see, so hands merge, backs are smooth, and faces are generic.
- Hollow shells decimate poorly; the simplifier falls back to a topology-preserving one and may keep more than 120k faces. That is correct behaviour, not a failure.
- Free cloud quota is per model. If one model returns `402`, try another before assuming the key is bad. See `references/troubleshooting.md`.
