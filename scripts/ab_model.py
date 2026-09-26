"""A/B a Gemini model on one episode: run the scene analysis with MODEL, save it alongside the cached one,
and print a side-by-side of what matters for brand safety and cut quality.

    python scripts/ab_model.py mohanagar gemini-3.8-flash
"""
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from adbreak import config, understand  # noqa: E402

vid, model = sys.argv[1], sys.argv[2]
work = ROOT / "out" / vid
config.GEMINI_MODEL = model
out = work / f"analysis_{model}.json"
if not out.exists():
    t0 = time.time()
    info = json.loads((work / "probe.json").read_text())
    analysis = understand.analyse(work / "proxy.mp4", info["duration"])
    analysis["wall_s"] = round(time.time() - t0, 1)
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")

base = json.loads((work / "analysis.json").read_text(encoding="utf-8"))
new = json.loads(out.read_text(encoding="utf-8"))


def summary(a):
    scenes = a["scenes"]
    return {
        "model": a.get("model"), "scenes": len(scenes),
        "avg_tags": round(sum(len(s.get("tags", [])) for s in scenes) / len(scenes), 1),
        "scenes_with_sensitive": sum(bool(s["sensitive_themes"]) for s in scenes),
        "sensitive_labels": sorted({t for s in scenes for t in s["sensitive_themes"]}),
        "usage": a.get("usage"), "wall_s": a.get("wall_s"),
    }


for a in (base, new):
    print(json.dumps(summary(a), ensure_ascii=False))
print()
w = max(len(a["scenes"]) for a in (base, new))
for i in range(w):
    row = []
    for a in (base, new):
        s = a["scenes"][i] if i < len(a["scenes"]) else None
        row.append(f"{s['start']}-{s['end']} {s['dominant_activity'][:38]:38s} [{','.join(s['sensitive_themes'])}]" if s else "")
    print(f"{row[0]:78s} | {row[1]}")
