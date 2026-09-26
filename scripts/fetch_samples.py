"""Download the sample episodes into data/videos if they are missing (they are not committed to git)."""
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
VIDEOS = ROOT / "data" / "videos"
VIDEOS.mkdir(parents=True, exist_ok=True)

samples = json.loads((ROOT / "samples.json").read_text(encoding="utf-8"))["videos"]
for vid, drive_id in samples.items():
    dest = VIDEOS / f"{vid}.mp4"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        continue
    url = f"https://drive.usercontent.google.com/download?id={drive_id}&export=download&confirm=t"
    print(f"fetching {vid}...", flush=True)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        print(f"  {dest.stat().st_size / 1e6:.0f} MB", flush=True)
    except Exception as e:  # a missing sample must not stop the server from starting
        print(f"  failed: {e}", file=sys.stderr, flush=True)
        dest.unlink(missing_ok=True)
