"""Assemble the frames recorded by web/e2e/demo.spec.ts into README GIFs (dev tool, needs Pillow).

    uv run --no-project --with pillow==11.3.0 python scripts/make_gif.py <frames dir> <out dir> [width] [colors]

Each scene folder holds PNG frames and frames.json (capture times in ms); the GIF keeps the real timing,
holds the last frame, drops identical consecutive frames and is scaled down to keep the file small.
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

WIDTH, COLORS = 960, 256
MIN_MS, END_HOLD_MS = 40, 2500


def build(scene: Path, out: Path, width: int = WIDTH, colors: int = COLORS) -> None:
    frames = json.loads((scene / "frames.json").read_text())
    images, durations, last = [], [], None
    for i, f in enumerate(frames):
        nxt = frames[i + 1]["t"] if i + 1 < len(frames) else f["t"] + END_HOLD_MS
        im = Image.open(f["file"]).convert("RGB")
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        if last is not None and ImageChops.difference(im, last).getbbox() is None:
            durations[-1] += nxt - f["t"]  # same picture: extend the previous frame
            continue
        images.append(im)
        durations.append(nxt - f["t"])
        last = im
    durations = [max(MIN_MS, round(d / 10) * 10) for d in durations]
    pal = [im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for im in images]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True, disposal=1)
    print(f"{out}: {len(pal)} frames, {sum(durations) / 1000:.1f}s, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    width = int(sys.argv[3]) if len(sys.argv) > 3 else WIDTH
    colors = int(sys.argv[4]) if len(sys.argv) > 4 else COLORS
    for scene in sorted(p for p in src.iterdir() if (p / "frames.json").exists()):
        build(scene, dst / f"{scene.name}.gif", width, colors)
