"""Hard spending guard for Gemini.

Every call is estimated before it is sent and refused if it would push total spend past GEMINI_BUDGET_USD.
Actual usage (per modality, from the API response) is appended to out/gemini_ledger.json after each call.
"""
import json
import os
import threading
import time
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "out" / "gemini_ledger.json"
_lock = threading.Lock()

# USD per 1M tokens: (text/image/video input, audio input, output incl. thinking).
# Unknown models fall back to a deliberately pessimistic price so the guard errs on the safe side.
PRICES = {
    "gemini-2.5-flash": (0.30, 1.00, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.30, 0.40),
    "gemini-3.8-flash": (0.75, 0.75, 3.75),  # promotional rate through Dec 2026; audio assumed same as video
}
FALLBACK_PRICE = (1.50, 1.50, 9.00)


class BudgetExceeded(RuntimeError):
    pass


def budget_usd() -> float:
    return float(os.getenv("GEMINI_BUDGET_USD", "10"))


def max_video_minutes() -> float:
    return float(os.getenv("GEMINI_MAX_VIDEO_MINUTES", "40"))


def _load() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"spent_usd": 0.0, "calls": []}


def spent() -> float:
    return _load()["spent_usd"]


def estimate(model: str, media_seconds: float, output_tokens: int) -> float:
    """Pessimistic pre-call estimate: ~100 tokens/s at low media resolution (+50% margin), all priced as audio."""
    _, audio_in, out = PRICES.get(model, FALLBACK_PRICE)
    input_tokens = media_seconds * 100 * 1.5 + 2_000
    return (input_tokens * audio_in + output_tokens * out) / 1e6


def check(purpose: str, estimated_usd: float) -> None:
    with _lock:
        total = spent()
        if total + estimated_usd > budget_usd():
            raise BudgetExceeded(
                f"Gemini budget guard: '{purpose}' needs ~${estimated_usd:.3f}, already spent ${total:.3f} "
                f"of ${budget_usd():.2f} (raise GEMINI_BUDGET_USD in .env to allow more)."
            )


def record(purpose: str, model: str, usage) -> float:
    text_in, audio_in, out = PRICES.get(model, FALLBACK_PRICE)
    by_modality = {}
    for d in getattr(usage, "prompt_tokens_details", None) or []:
        by_modality[str(getattr(d.modality, "name", d.modality))] = d.token_count or 0
    prompt = usage.prompt_token_count or 0
    audio = by_modality.get("AUDIO", 0)
    output = (usage.candidates_token_count or 0) + (getattr(usage, "thoughts_token_count", None) or 0)
    cost = ((prompt - audio) * text_in + audio * audio_in + output * out) / 1e6
    with _lock:
        ledger = _load()
        ledger["spent_usd"] = round(ledger["spent_usd"] + cost, 6)
        ledger["calls"].append({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "purpose": purpose, "model": model,
            "prompt_tokens": prompt, "by_modality": by_modality, "output_tokens": output, "usd": round(cost, 6),
        })
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    return cost
