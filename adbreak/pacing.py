"""Pacing (the "whether"): choose the set of breaks that maximises total break quality while spreading them
evenly, then allocate a creative duration per break, all under hard rules: max breaks for the runtime,
min gap between breaks, max ad load (real creative seconds / runtime)."""
from itertools import combinations

from . import config

EVENNESS_WEIGHT = 1.0
MAX_POOL = 25


def max_breaks_for(duration: float) -> int:
    return max(1, round(duration / 3600 * config.MAX_BREAKS_PER_HOUR))


def ad_budget_s(duration: float) -> float:
    return duration * config.MAX_AD_LOAD_PCT / 100


def _feasible(ts: list[float], duration: float, unit_s: float) -> bool:
    if any(b - a < config.MIN_GAP_BETWEEN_BREAKS_S for a, b in zip(ts, ts[1:])):
        return False
    return len(ts) * unit_s <= ad_budget_s(duration)


def objective(combo: tuple[dict, ...], duration: float) -> float:
    k = len(combo)
    ideal = [duration * (i + 1) / (k + 1) for i in range(k)]
    unevenness = sum(abs(c["t"] - x) for c, x in zip(combo, ideal)) / duration
    return sum(c["quality"] for c in combo) - EVENNESS_WEIGHT * unevenness


def plan(pool: list[dict], duration: float, unit_s: float) -> list[dict]:
    """Best-spaced feasible subset, assuming the shortest creative in the catalogue per break."""
    pool = sorted(sorted(pool, key=lambda c: -c["quality"])[:MAX_POOL], key=lambda c: c["t"])
    best, best_score = [], float("-inf")
    for k in range(1, min(max_breaks_for(duration), len(pool)) + 1):
        for combo in combinations(pool, k):
            if not _feasible([c["t"] for c in combo], duration, unit_s):
                continue
            score = objective(combo, duration)
            if score > best_score:
                best, best_score = list(combo), score
    return best


def _pick(creatives: list[dict], target_s: int) -> dict:
    exact = [c for c in creatives if c["duration_sec"] == target_s]
    return exact[0] if exact else creatives[0]


def _neighbour(creatives: list[dict], current: dict, longer: bool) -> dict | None:
    ordered = sorted(creatives, key=lambda c: c["duration_sec"])
    i = ordered.index(current)
    j = i + 1 if longer else i - 1
    return ordered[j] if 0 <= j < len(ordered) else None


def allocate_creatives(breaks: list[dict], duration: float) -> bool:
    """Start every break at the preferred length; shorten the weakest breaks until under the ad-load cap;
    then lengthen the strongest ones while headroom remains. Returns False if the cap cannot be met."""
    budget = ad_budget_s(duration)
    for b in breaks:
        b["creative"] = _pick(b["brand"]["creatives"], config.PREFERRED_CREATIVE_S)

    def total() -> int:
        return sum(b["creative"]["duration_sec"] for b in breaks)

    while total() > budget:
        options = [(b, _neighbour(b["brand"]["creatives"], b["creative"], longer=False)) for b in breaks]
        options = [(b, s) for b, s in options if s]
        if not options:
            return False
        b, shorter = min(options, key=lambda bs: bs[0]["quality"])
        b["creative"] = shorter

    for b in sorted(breaks, key=lambda b: -b["quality"]):
        if b["quality"] < config.LONG_CREATIVE_MIN_QUALITY:
            break
        longer = _neighbour(b["brand"]["creatives"], b["creative"], longer=True)
        if longer and total() - b["creative"]["duration_sec"] + longer["duration_sec"] <= budget:
            b["creative"] = longer
    return True
