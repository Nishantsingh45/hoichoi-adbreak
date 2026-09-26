"""Turn Gemini's scene boundaries into frame-accurate, speech-free cut candidates (the "where").

Gemini's timestamps are ~1s granular, so they only nominate a *region*. The actual cut point must be a
fade-to-black, a real shot change, or a long pause, with no speech on either side; that is signal processing.
"""
import numpy as np

from . import config
from .understand import to_seconds
from .vad import max_prob

PRIORITY = {"fade_to_black": 0, "shot_cut": 1, "pause": 2}


def silence_gaps(speech: list[list[float]], duration: float) -> list[tuple[float, float]]:
    gaps, prev_end = [], 0.0
    for start, end in speech:
        if start > prev_end:
            gaps.append((prev_end, start))
        prev_end = max(prev_end, end)
    if duration > prev_end:
        gaps.append((prev_end, duration))
    return gaps


def _gap_containing(t: float, gaps):
    for g in gaps:
        if g[0] <= t <= g[1]:
            return g
    return None


def snap(t: float, shots: list[dict], blacks: list[list[float]], window: float = 3.0) -> float:
    """Nearest real visual boundary to a Gemini timestamp, for display; unchanged if none is close."""
    points = [s["t"] for s in shots] + [(b[0] + b[1]) / 2 for b in blacks]
    near = [p for p in points if abs(p - t) <= window]
    return round(min(near, key=lambda p: abs(p - t)), 3) if near else t


def build(scenes: list[dict], shots: list[dict], blacks: list[list[float]], speech: list[list[float]],
          probs: np.ndarray, duration: float) -> tuple[list[dict], list[dict]]:
    gaps = silence_gaps(speech, duration)
    clearance = config.MIN_SPEECH_CLEARANCE_S
    candidates, rejected = [], []

    def quiet(t: float) -> bool:
        return max_prob(probs, t - clearance, t + clearance) < config.CUT_MAX_SPEECH_PROB

    def option(t: float, kind: str, score: float, gap) -> dict:
        return {"t": round(t, 3), "kind": kind, "shot_score": score,
                "silence_before_s": round(t - gap[0], 2), "silence_after_s": round(gap[1] - t, 2)}

    for i in range(len(scenes) - 1):
        before, after = scenes[i], scenes[i + 1]
        boundary = to_seconds(after["start"])
        base = {"boundary_index": i, "gemini_boundary": round(boundary, 2)}

        if boundary < config.NO_BREAK_BEFORE_S or boundary > duration - config.NO_BREAK_IN_LAST_S:
            rejected.append({**base, "reason": "inside protected opening/ending window"})
            continue

        lo, hi = boundary - config.BOUNDARY_SEARCH_WINDOW_S, boundary + config.BOUNDARY_SEARCH_WINDOW_S
        options = []

        # Best: the middle of a fade-to-black (the edit already pauses the story there).
        for b0, b1 in blacks:
            t = (b0 + b1) / 2
            gap = _gap_containing(t, gaps)
            if lo <= t <= hi and gap and quiet(t):
                options.append(option(t, "fade_to_black", round(b1 - b0, 2), gap))

        # Good: a hard shot cut inside a speech-free gap, with clearance on both sides.
        for shot in shots:
            if not lo <= shot["t"] <= hi:
                continue
            gap = _gap_containing(shot["t"], gaps)
            if gap and shot["t"] - gap[0] >= clearance and gap[1] - shot["t"] >= clearance and quiet(shot["t"]):
                options.append(option(shot["t"], "shot_cut", shot["score"], gap))

        # Fallback: the middle of a long pause, when the edit has no hard cut (e.g. a dissolve).
        if not options:
            for g in gaps:
                s, e = max(g[0], lo), min(g[1], hi)
                t = (s + e) / 2
                if e - s >= config.MIN_SOFT_GAP_S and g[1] - g[0] >= config.MIN_SOFT_GAP_S and quiet(t):
                    options.append(option(t, "pause", 0.0, g))

        if not options:
            rejected.append({**base, "reason": "speech continues across the whole boundary region (would cut mid-dialogue)"})
            continue

        # Fade beats shot cut beats pause; then closest to the semantic boundary; longer silence breaks ties.
        # Runners-up are kept as fallbacks in case the audio audit hears speech at the preferred cut.
        options.sort(key=lambda o: (PRIORITY[o["kind"]], abs(o["t"] - boundary),
                                    -(o["silence_before_s"] + o["silence_after_s"])))
        candidates.append({
            **base, **options[0],
            "alternatives": options[1:3],
            "id": f"c{i:03d}",
            "scene_before": before,
            "scene_after": after,
        })
    return candidates, rejected
