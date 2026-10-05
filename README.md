# StitchBitch

Turn images and SVGs into embroidery files. Local CLI, no cloud.

## What it does

- **Raster** (PNG/JPG/...): **1:1 color mapping** — every image color gets a
  thread of the same color, white included. The palette is learned only from
  flat pixels (a widened edge filter excludes anti-aliasing, so blends don't
  become extra threads / speckles). The background connected to the image
  border is removed while **interior white is kept** (e.g. white text);
  `--keep-background` stitches the background as well.
  **Color merge**: colors within ±5% (per channel, configurable) are merged into
  one entry whose value is the pixel-weighted average (AVG) — this removes color
  artifacts from both image and stitch file. Then stitched as a serpentine/hatch
  fill. **Self-check**: the color count is raised automatically until the thread
  colors match the image; the match score is reported.
  **Transparency stays transparency**: only opaque pixels are analyzed and
  stitched — transparent is no color and is never stitched.
- **SVG**: filled paths are filled, strokes are stitched as running lines on top.
  Transforms, `viewBox`, and holes (even-odd) are supported.
- **Output**: any [pyembroidery](https://github.com/EmbroidePy/pyembroidery)
  format; the file extension decides: `.dst` (Tajima, default), `.pes` (Brother),
  `.jef` (Janome), `.exp`, `.vp3`, `.pec`, `.xxx`, `.u01`, ...

## Setup

```bash
cd ./StitchBitch
./run.sh --help                 # first run creates venv + installs, then CLI
./run.sh --serve                # web UI on http://127.0.0.1:8000
```

Manual (without run.sh):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Sharing it

Nothing to compile (pure Python). Share the folder or a tarball:

```bash
# run from the parent directory
tar --exclude=.venv --exclude=__pycache__ --exclude=.git -czf StitchBitch.tar.gz StitchBitch
```

The other person unpacks it, needs Python 3 + internet (first run only), then:

```bash
./run.sh --serve        # then open http://127.0.0.1:8000
./run.sh image.png -o image.dst
```

## Usage

```bash
.venv/bin/python stitchbitch.py logo.png -o logo.dst
.venv/bin/python stitchbitch.py logo.svg -o logo.pes --width 80
.venv/bin/python stitchbitch.py photo.jpg -o photo.dst --colors 16 --width 120 \
    --stitch 2.5 --row 0.4 --angle 45 --preview photo.png
```

| Option | Default | Meaning |
|---|---|---|
| `-o/--output` | input with `.dst` | output file; extension = format |
| `--width` | `100` | finished width in mm |
| `--height` | `0` (ignore) | max height in mm |
| `--colors` | `16` | number of colors for raster images (up to 64 useful) |
| `--merge-color` | `5` | merge colors within ±X% (value = average); `0` = off |
| `--stitch` | auto | stitch length in mm (default `width/120`, 0.5–3.0) |
| `--row` | auto | fill row spacing in mm (default `stitch/4`, 0.25–0.5) |
| `--angle` | `45` | fill angle in degrees (default for all colors) |
| `--angle-color RRGGBB=DEG` | – | fill angle for one color, e.g. `e31e24=90` (repeatable) |
| `--rotate` | `0` | rotate the whole design by degrees (width applies before rotation) |
| `--ignore-color RRGGBB` | – | skip this color entirely, repeatable |
| `--keep-background` | – | do NOT remove the border-connected background (1:1, stitch everything) |
| `--outline` | – | clean edges: stitch a smoothed outline along each region edge |
| `--border-color RRGGBB` | – | patch border along the motif silhouette in this color |
| `--border-width` | `3` | border thickness in mm; `--width` includes the border |
| `--preview` | – | also write a preview PNG |

### Realistic preview

The preview is not a plain line drawing: every stitch is rendered as a thread
with a sheen/highlight and a soft drop shadow on a subtle fabric background,
supersampled for smooth edges — so it looks close to the real embroidery.
(`--preview out.png`, and the web preview.)

### Clean edges ("over-stitch the border")

With `--outline` (web: **Clean edges** checkbox) a smoothed outline is stitched
along each color region (contours are Chaikin-smoothed, so no pixel staircase).
Use it when edges look ragged and you want a crisp, stitched border.

### Connected stitching (few jumps/cuts)

Stitches are ordered so connected areas are sewn as one thread. A gap is only
over-stitched when the connecting path lies within the **same color** (invisible,
same thread); any gap that would run across another color is jumped and cut — so
no thread runs visibly across the design. This also cuts the number of cuts/jumps
from ~1500 per logo down to a small number.

### Same accuracy at every size

Stitch density adapts to the finished width: the target is ~120 stitches across
the width. Small motifs get short stitches and tight rows, large ones longer
ones — so the result is equally detailed regardless of `--width`. Override with
`--stitch`/`--row`. Lower bound: ~0.5 mm stitch length; physically it cannot get
finer than that.

## Web UI

```bash
.venv/bin/python stitchbitch.py --serve            # http://127.0.0.1:8000
.venv/bin/python stitchbitch.py --serve --port 9000
```

Open the browser, choose a file, set the parameters, **Stitch**, then view the
preview and download the stitch file.

- **Rotate**: slider below the image; the preview rotates live, the exported file
  is rotated accordingly.
- **Palette**: after choosing a file the colors appear as tiles (with pixel
  counts; the likely background is marked `(BG)`). The background is **unchecked**
  by default (only the border-connected area disappears, interior white stays).
  Check it again for 1:1. Unchecking other colors removes them entirely.
  Transparent areas never show up (they are no color).
- **Per-color angle**: each tile has a small `°` field for that color's own fill
  angle; empty = the global angle.
- **Clean edges**: checkbox that stitches a smoothed outline along the region
  edges (crisper borders).
- **Layout**: drag the divider to resize the sidebar, `⇔` moves the sidebar to
  the other side, `⛶` shows the workspace only. These settings are saved in the
  browser.

### Image editor (in the browser, before stitching)

Tab **Edit** next to **Preview**. Everything runs client-side on a canvas; the
server just receives the edited PNG:

- **Zoom/Pan**: wheel = zoom at cursor, `+`/`−`/`Fit`, drag with ✋.
- **🩹 Eraser (scalpel)**: rub out areas (e.g. white bars) → becomes transparent
  and is therefore not stitched.
- **🖌️ Brush**: paint with the target color (add areas).
- **💧 Eyedropper**: pick the color under the cursor (also sets the replace source).
- **Replace color**: swap the picked color globally for the target color
  (e.g. white → red), with anti-aliasing tolerance.
- **↶ Undo**, **Reset** (original), **Size** = brush radius.
- **Patch border**: checkbox, thickness in mm and color. Adds a border along the
  **motif silhouette** (not a rectangle); the set width counts as the total size,
  border included.
- After editing, use **Refresh palette** if needed; then **Stitch**.

No framework (Python standard library only, `http.server`) and no upload to any
server — everything is local. Bound to `127.0.0.1` only, no authentication.

## Limits (deliberate, v1)

- Only one fill type (hatch). No satin, no underlay, no pull compensation, no
  per-shape stitch direction. Good for logos/flat colors, still coarse for
  proper lettering/fine detail.
- SVG stroke width is ignored: a thick stroke becomes a single running line.
- Match score = color hits per flat pixel, not a pixel diff of the rendered
  stitches. Below 100% means the palette could not reproduce the image exactly
  (e.g. photos/gradients).
- Background detection = most common opaque border color (only if >15% area); it
  is just a `(BG)` hint and drives `--keep-background`.
- Raster edges follow the pixel grid; very large images are downscaled to 2000 px
  (analysis is intentionally thorough and takes a few seconds).
- Color distance is RGB, not CIELAB (no perceptual color model).
- The color-merge threshold is per channel (max channel difference), not Euclidean.
- Thread colors = image colors, no matching against a real thread palette.

Possible next steps if needed: satin/underlay for better quality, thread palette
matching, multiple motifs per file.
