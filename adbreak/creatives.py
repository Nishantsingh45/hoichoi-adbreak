"""Placeholder creatives, rendered on demand at the exact path and duration the catalogue specifies, so a
real ad file dropped at that path is used untouched and any new brand gets a creative automatically."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .media import run

FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int):
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def _center(draw, text, y, font, fill):
    w = draw.textlength(text, font=font)
    draw.text(((1280 - w) / 2, y), text, font=font, fill=fill)


def local_path(out_root: Path, creative: dict) -> Path | None:
    """Where the creative lives under out/, or None if the catalogue points at a remote URL."""
    url = creative["url"]
    if url.startswith(("http://", "https://")):
        return None
    rel = Path(url.lstrip("/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe creative path: {url}")
    return out_root / rel


def ensure(brand: dict, creative: dict, out_root: Path) -> Path | None:
    mp4 = local_path(out_root, creative)
    if mp4 is None or mp4.exists():
        return mp4
    mp4.parent.mkdir(parents=True, exist_ok=True)
    png = mp4.with_suffix(".png")
    duration = creative["duration_sec"]
    img = Image.new("RGB", (1280, 720), brand.get("color", "#333333"))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 1220, 660], outline="white", width=4)
    _center(d, brand["name"], 230, _font(96), "white")
    _center(d, brand.get("category", ""), 370, _font(40), "white")
    _center(d, f"{creative['id']} · {duration}s · {creative.get('language', 'bn')}", 450, _font(28), "#f0f0f0")
    _center(d, "Placeholder creative · synthetic brand", 600, _font(22), "#e0e0e0")
    img.save(png)
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", "25", "-i", str(png),
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(duration),
         "-vf", f"fade=in:0:10,fade=out:st={duration - 0.5}:d=0.5,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(mp4)])
    png.unlink(missing_ok=True)
    return mp4
