"""End-to-end: video in -> scenes -> scored cut candidates -> paced, brand-safe breaks -> VMAP + debug JSON.

Every expensive step is cached in out/<video_id>/, so policy or catalogue changes re-run in seconds.

Usage:
    python -m adbreak.pipeline data/videos/bhojon_bilashi.mp4 [--brands brands/brands.json ...] [--redo judge]
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from . import brands as brand_lib
from . import candidates as cand
from . import budget, config, creatives, media, vmap

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
DEFAULT_BRANDS = [ROOT / "brands" / "brands.json"]
STEPS = ["signals", "understand", "judge", "select"]


def load_brands(paths: list[Path]) -> list[dict]:
    return brand_lib.load(paths)


def _cached(path: Path, redo: bool, fn):
    if path.exists() and not redo:
        return json.loads(path.read_text(encoding="utf-8"))
    value = fn()
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    return value


def select_breaks(cands: list[dict], brands: list[dict], synopsis: str, duration: float, video: Path,
                  work: Path, log) -> list[dict]:
    """WHERE/WHETHER: plan the best-spaced set of breaks, audit each planned cut (falling back to nearby
    alternative cuts), and WHAT: match a brand. A break whose cuts all fail the audit, or where no catalogue
    brand is safe and relevant, does not survive; the planner then picks the next-best set."""
    from . import judge, pacing, verify

    audits_path = work / "audits.json"
    audits = json.loads(audits_path.read_text(encoding="utf-8")) if audits_path.exists() else {}

    def audit(t: float) -> dict:
        key = f"{t:.3f}"
        if key not in audits:
            audits[key] = verify.audit_cut(video, t)
            audits_path.write_text(json.dumps(audits, indent=1), encoding="utf-8")
        return audits[key]

    def verify_cut(c: dict) -> bool:
        for option in [None] + c.get("alternatives", []):
            if option:
                c["rejected_cuts"] = c.get("rejected_cuts", []) + [{"t": c["t"], "heard": c["audit"]["what_is_heard"]}]
                c.update({k: option[k] for k in ("t", "kind", "shot_score", "silence_before_s", "silence_after_s")})
            c["audit"] = audit(c["t"])
            if c["audit"]["passed"]:
                return True
        return False

    pool = []
    for c in cands:
        if c["jev"]["same_conversation"] >= config.SAME_CONVERSATION_REJECT:
            c["decision"] = "rejected: Jev says the same conversation continues across the cut"
        elif c["quality"] < config.MIN_BREAK_QUALITY:
            c["decision"] = f"rejected: quality {c['quality']:.2f} below {config.MIN_BREAK_QUALITY}"
        else:
            pool.append(c)

    unit_s = brand_lib.min_creative_s(brands)
    verified = set()
    while True:
        chosen = pacing.plan(pool, duration, unit_s)
        failed = None
        for c in chosen:
            if c["id"] in verified:
                continue
            if not verify_cut(c):
                c["decision"] = f"rejected: Gemini audit heard dialogue at every clean cut here ({c['audit']['what_is_heard']})"
                failed = c
                break
            c["brand_match"] = judge.match_brand(c, brands, synopsis)
            if c["brand_match"]["choice"] is None:
                if config.UNSAFE_SLOT_POLICY == "house_promo":
                    c["brand_match"]["fallback"] = "house_promo"
                else:
                    blocked = [row["brand"] for row in c["brand_match"]["table"] if row["blocked"]]
                    floor = c["brand_match"]["floor"]
                    c["decision"] = (f"rejected: platform brand-safety floor ({'; '.join(floor)})" if floor else
                                     f"rejected: no catalogue brand is safe and relevant here "
                                     f"({len(blocked)}/{len(brands)} blocked by negative contexts)")
                    failed = c
                    break
            verified.add(c["id"])
        if failed is None:
            break
        log(f"  {failed['id']} t={failed['t']:7.2f} -> {failed['decision']}; re-planning")
        pool.remove(failed)

    chosen_ids = {c["id"] for c in chosen}
    for c in pool:
        c["decision"] = "selected" if c["id"] in chosen_ids else \
            "not chosen: the pacing planner preferred a better-spaced set of breaks"
    for c in sorted(cands, key=lambda c: c["t"]):
        log(f"  {c['id']} t={c['t']:7.2f} q={c['quality']:.2f} -> {c['decision']}")
    return sorted(chosen, key=lambda c: c["t"])


def trace(b: dict, brands_total: int) -> dict:
    """The three questions the brief asks, with the evidence that decided each, for the debug JSON."""
    m = b["brand_match"]
    choice = m["choice"]
    return {
        "where": {
            "cut_s": b["t"], "cut_type": b["kind"],
            "gemini_scene_boundary_s": b["gemini_boundary"],
            "silence_before_s": b["silence_before_s"], "silence_after_s": b["silence_after_s"],
            "audio_audit": b["audit"]["what_is_heard"],
            "moved_from": b.get("rejected_cuts", []),
        },
        "whether": {
            "quality": b["quality"],
            "jev_natural_break_0_to_3": b["jev"]["natural_break"],
            "jev_p_same_conversation": b["jev"]["same_conversation"],
            "jev_p_emotional_peak": b["jev"]["emotional_peak"],
            "creative_seconds": b["creative"]["duration_sec"],
        },
        "what": {
            "brand": b["brand"]["name"], "creative": b["creative"]["id"],
            "fit_0_to_3": choice["fit"] if choice else None,
            "p_forbidden_context": choice["p_forbidden_context"] if choice else None,
            "platform_floor": m["floor"],
            "blocked_brands": [{"brand": r["brand"], "blocked_by": r["blocked_by"], "evidence": r["lexical_evidence"],
                                "p_forbidden_context": r["p_forbidden_context"]} for r in m["table"] if r["blocked"]],
            "brands_considered": brands_total,
        },
    }


def process(video: Path, brand_files: list[Path] | None = None, redo: set[str] = frozenset(), log=print) -> dict:
    from . import pacing, understand, vad

    load_dotenv(ROOT / ".env")
    vid = Path(str(video).split("?")[0]).stem  # works for a local path or a remote (R2) URL
    work = OUT / vid
    work.mkdir(parents=True, exist_ok=True)
    timings = {}
    t_start = time.time()

    info = _cached(work / "probe.json", False, lambda: media.probe(media.src(video)))
    duration = info["duration"]
    if duration > budget.max_video_minutes() * 60:
        raise ValueError(f"This video is {duration / 60:.0f} min; the limit is {budget.max_video_minutes():.0f} min.")

    def signals():
        t0 = time.time()
        probs_path = work / "speech_probs.npy"
        if not probs_path.exists() or "signals" in redo:
            wav = work / "audio.wav"
            media.extract_audio(media.src(video), wav)
            np.save(probs_path, vad.speech_probs(wav))
            wav.unlink(missing_ok=True)  # the probability curve is all we keep; saves disk
        probs = np.load(probs_path)
        speech = _cached(work / "speech.json", "signals" in redo, lambda: vad.segments(probs))
        shots_path, black_path = work / "shots.json", work / "black.json"
        if "signals" in redo or not (shots_path.exists() and black_path.exists()):
            shots, blacks = media.detect_shots_and_black(video, work)
            shots_path.write_text(json.dumps(shots), encoding="utf-8")
            black_path.write_text(json.dumps(blacks), encoding="utf-8")
        shots = json.loads(shots_path.read_text(encoding="utf-8"))
        blacks = json.loads(black_path.read_text(encoding="utf-8"))
        timings["signals_s"] = round(time.time() - t0, 1)
        log(f"[signals] {len(shots)} shot cuts, {len(blacks)} fades to black, {len(speech)} speech segments "
            f"({timings['signals_s']}s)")
        return probs, speech, shots, blacks

    def understand_step():
        t0 = time.time()
        proxy = work / "proxy.mp4"
        if not proxy.exists():
            log("[understand] encoding a small proxy for Gemini...")
            media.make_proxy(media.src(video), proxy)
        analysis = _cached(work / "analysis.json", "understand" in redo,
                           lambda: understand.analyse(proxy, duration, log))
        timings["understand_s"] = round(time.time() - t0, 1)
        log(f"[understand] {len(analysis['scenes'])} scenes ({timings['understand_s']}s)")
        return analysis

    # Local signal extraction and the Gemini pass are independent; run them side by side.
    with ThreadPoolExecutor(max_workers=1) as pool_:
        signals_future = pool_.submit(signals)
        analysis = understand_step()
        probs, speech, shots, blacks = signals_future.result()

    t0 = time.time()

    def _judge():
        from . import judge
        cs, rej = cand.build(analysis["scenes"], shots, blacks, speech, probs, duration)
        judge.judge_breaks(cs, analysis["synopsis"])
        return {"candidates": cs, "rejected": rej}

    judged = _cached(work / "candidates.json", "judge" in redo or "understand" in redo, _judge)
    timings["judge_s"] = round(time.time() - t0, 1)
    log(f"[judge] {len(judged['candidates'])} clean cut candidates, {len(judged['rejected'])} boundaries rejected")

    t0 = time.time()
    brands = load_brands(brand_files or DEFAULT_BRANDS)
    breaks = select_breaks(judged["candidates"], brands, analysis["synopsis"], duration, video, work, log)

    by_id = {b["id"]: b for b in brands}
    house = brand_lib.normalise(config.HOUSE_PROMO)
    for b in breaks:
        choice = b["brand_match"]["choice"]
        b["brand"] = by_id[choice["brand_id"]] if choice else house
    while breaks and not pacing.allocate_creatives(breaks, duration):
        weakest = min(breaks, key=lambda b: b["quality"])
        weakest["decision"] = "dropped: even the shortest creatives would exceed the ad-load cap"
        log(f"  {weakest['id']} t={weakest['t']:7.2f} -> {weakest['decision']}")
        breaks.remove(weakest)
    timings["select_s"] = round(time.time() - t0, 1)

    for b in breaks:
        creatives.ensure(b["brand"], b["creative"], OUT)
        url = b["creative"]["url"]
        if url.startswith(("http://", "https://")):
            b["creative_url"] = b["media_url"] = url
        else:
            b["creative_url"] = "/" + url.lstrip("/")
            b["media_url"] = config.CREATIVE_BASE_URL.rstrip("/") + "/" + url.lstrip("/")
        b["trace"] = trace(b, len(brands))

    scenes = []
    for s in analysis["scenes"]:
        s = dict(s)
        s["display_start_s"] = cand.snap(understand.to_seconds(s["start"]), shots, blacks)
        s["display_end_s"] = cand.snap(understand.to_seconds(s["end"]), shots, blacks)
        scenes.append(s)

    (work / "vmap.xml").write_text(vmap.build(breaks), encoding="utf-8")
    ad_seconds = sum(b["creative"]["duration_sec"] for b in breaks)
    timings["total_s"] = round(time.time() - t_start, 1)
    result = {
        "video_id": vid,
        "video_url": f"/videos/{vid}.mp4",
        "duration": duration,
        "synopsis": analysis["synopsis"],
        "scenes": scenes,
        "breaks": breaks,
        "candidates": judged["candidates"],
        "rejected_boundaries": judged["rejected"],
        "brands": brands,
        "policy": {k: getattr(config, k) for k in dir(config) if k.isupper() and k != "HOUSE_PROMO"},
        "pacing": {
            "max_breaks_for_runtime": pacing.max_breaks_for(duration),
            "ad_budget_s": round(pacing.ad_budget_s(duration), 1),
            "ad_seconds_used": ad_seconds,
            "ad_load_pct": round(100 * ad_seconds / duration, 2),
        },
        "analysis_model": analysis.get("model"),
        "analysed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((work / "analysis.json").stat().st_mtime)),
        "timings": timings,
        "vmap_url": f"/out/{vid}/vmap.xml",
    }
    (work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [v for v in index if v["video_id"] != vid] + [{"video_id": vid, "duration": duration, "breaks": len(breaks)}]
    index_path.write_text(json.dumps(sorted(index, key=lambda v: v["video_id"]), indent=1), encoding="utf-8")
    log(f"[done] {len(breaks)} breaks, ad load {result['pacing']['ad_load_pct']}% in {timings['total_s']}s "
        f"-> out/{vid}/result.json, vmap.xml")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--brands", type=Path, nargs="+", default=DEFAULT_BRANDS)
    ap.add_argument("--redo", nargs="*", default=[], choices=STEPS)
    args = ap.parse_args()
    process(args.video, args.brands, set(args.redo))


if __name__ == "__main__":
    main()
