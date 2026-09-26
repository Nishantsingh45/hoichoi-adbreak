"""Brand catalogue in the official hackathon schema (brand_id, display_name, category, target_contexts,
negative_contexts, creatives[]) plus the deterministic negative-context check that backs up Jev.

A new brand needs no code: it is judged from its own contexts, and its creatives get placeholders on demand.
"""
import colorsys
import hashlib
import json
import re
from pathlib import Path

# Evidence words for common negative contexts. A context missing from this map (a 9th brand may bring one)
# still matches its own words, so recall degrades gracefully rather than failing open.
CONTEXT_CUES = {
    "funeral": ["funeral", "mourning", "mourners", "cremation", "shraddha", "condolence", "dead body", "corpse", "last rites", "death"],
    "grief": ["grief", "grieving", "mourning", "bereaved", "bereavement", "wailing", "sobbing over"],
    "hospital": ["hospital", "clinic", "icu", "hospital ward", "doctor", "nurse", "patient", "stretcher"],
    "medical emergency": ["medical emergency", "ambulance", "emergency room", "collapses", "heart attack", "stroke", "unconscious"],
    "illness": ["illness", "ill", "sick", "sickness", "fever", "disease", "vomit", "vomiting", "unwell", "coughing"],
    "injury": ["injury", "injured", "wound", "wounded", "bleeding", "blood", "fracture"],
    "accident": ["accident", "crash", "collision", "run over"],
    "violence": ["violence", "violent", "fight", "fighting", "beating", "beaten", "assault", "gun", "gunshot", "shooting",
                 "stabbing", "stabbed", "murder", "killing", "attack", "slap", "slapped", "torture", "hostage"],
    "financial distress": ["financial distress", "debt", "loan shark", "bankrupt", "bankruptcy", "poverty", "cannot pay",
                           "unpaid", "eviction", "moneylender", "extortion"],
    "eating": ["eating", "eats", "meal", "dinner", "lunch", "breakfast", "feast", "feeding", "snacking", "dining"],
    "bathroom": ["bathroom", "toilet", "washroom", "lavatory", "shower", "bathing"],
    "smoking": ["smoking", "smokes", "cigarette", "bidi", "hookah"],
    "alcohol": ["alcohol", "drinking alcohol", "drunk", "liquor", "whisky", "beer", "wine", "bar"],
}
# Platform-level floor: contexts no advertiser in the catalogue should follow, whatever its own list says.
PLATFORM_FLOOR = {
    "sexual content": ["sexual content", "sex worker", "sex workers", "brothel", "red-light", "red light district",
                       "prostitution", "prostitute", "nudity", "sexual assault", "rape", "molestation"],
    "illegal drugs": ["illegal drugs", "drug deal", "drug dealer", "narcotics", "heroin", "cocaine", "drugs",
                      "weed", "ganja", "cannabis", "drug use"],
}
# Only short, structured fields are scanned so a stray word in a long summary does not block a brand;
# Jev reads the full summary and covers the rest. The scene after the break counts only by its opening.
SCAN_FIELDS = ("tags", "sensitive_themes", "dominant_activity", "location", "mood")
AFTER_FIELDS = ("dominant_activity", "location", "opening_gist")


def _color(bid: str) -> str:
    hue = int(hashlib.md5(bid.encode()).hexdigest()[:4], 16) / 0xFFFF
    r, g, b = colorsys.hls_to_rgb(hue, 0.38, 0.55)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")[:40]


def normalise(raw: dict) -> dict:
    """Accepts the official schema (or our earlier internal one) and returns one internal shape."""
    bid = raw.get("brand_id") or raw.get("id")
    name = raw.get("display_name") or raw.get("name")
    if not bid or not name:
        raise ValueError("a brand needs brand_id and display_name")
    bid = safe_id(bid)
    creatives = raw.get("creatives") or [
        {"id": f"{bid}_20s_bn", "duration_sec": 20, "language": "bn", "url": f"ads/{bid}/{bid}_20s_bn.mp4"}
    ]
    creatives = [{
        "id": str(c.get("id") or f"{bid}_{c['duration_sec']}s"),
        "duration_sec": int(c["duration_sec"]),
        "language": c.get("language", "bn"),
        "url": str(c.get("url") or f"ads/{bid}/{bid}_{c['duration_sec']}s.mp4"),
    } for c in creatives]
    return {
        "id": bid,
        "name": str(name),
        "category": str(raw.get("category", "")),
        "target_contexts": [str(x) for x in raw.get("target_contexts") or []],
        "negative_contexts": [str(x) for x in raw.get("negative_contexts") or []],
        "creatives": sorted(creatives, key=lambda c: c["duration_sec"]),
        "color": raw.get("color") or _color(bid),
    }


def load(paths: list[Path]) -> list[dict]:
    brands: dict[str, dict] = {}
    for p in paths:
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("brands", [])
        for raw in items:
            b = normalise(raw)
            brands[b["id"]] = b  # later files override earlier ones
    return list(brands.values())


def _scene_text(scene: dict, fields) -> str:
    parts = []
    for f in fields:
        v = scene.get(f)
        if isinstance(v, list):
            parts += [str(x) for x in v]
        elif v:
            parts.append(str(v))
    return " ".join(parts).lower().replace("_", " ")


def break_text(before: dict, after: dict) -> str:
    """The evidence a break is judged on: the whole scene before it, and only the opening of the scene after."""
    return _scene_text(before, SCAN_FIELDS) + " " + _scene_text(after, AFTER_FIELDS)


def _matches(contexts: dict[str, list[str]], text: str) -> list[str]:
    hits = []
    for ctx, cues in contexts.items():
        for cue in cues:
            if re.search(rf"\b{re.escape(cue)}\b", text):
                hits.append(f"{ctx} (matched '{cue}')")
                break
    return hits


def lexical_blocks(brand: dict, before: dict, after: dict) -> list[str]:
    """Deterministic layer: which of the brand's negative contexts have direct evidence in the scene notes."""
    contexts = {ctx: [ctx.lower().strip()] + CONTEXT_CUES.get(ctx.lower().strip(), []) for ctx in brand["negative_contexts"]}
    return _matches(contexts, break_text(before, after))


def floor_blocks(before: dict, after: dict) -> list[str]:
    """Platform floor, applied to every brand."""
    return _matches(PLATFORM_FLOOR, break_text(before, after))


def min_creative_s(brands: list[dict]) -> int:
    return min(c["duration_sec"] for b in brands for c in b["creatives"])
