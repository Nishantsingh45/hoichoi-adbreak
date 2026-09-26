"""Speech activity detection with Silero VAD (robust to background music, unlike plain silence detection).

We keep the full per-frame speech probability curve (31.25 frames/s), not just segments, so cut points can be
held to a stricter "no plausible speech at all" rule than the segmentation threshold.
"""
import wave
from pathlib import Path

import numpy as np
import torch
from silero_vad import load_silero_vad

from . import config

FRAME_S = 512 / 16000
_model = None


def speech_probs(wav_path: Path) -> np.ndarray:
    global _model
    if _model is None:
        _model = load_silero_vad()
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = torch.from_numpy(pcm.astype(np.float32) / 32768.0)
    _model.reset_states()
    with torch.no_grad():
        return _model.audio_forward(audio, 16000).squeeze(0).numpy().astype(np.float32)


def segments(probs: np.ndarray, on: float = config.VAD_THRESHOLD, off: float | None = None,
             min_silence_s: float = 0.15, pad_s: float = 0.06) -> list[list[float]]:
    """Hysteresis segmentation of the probability curve into speech segments (seconds)."""
    off = on - 0.15 if off is None else off
    segs, start, below = [], None, 0
    for i, p in enumerate(probs):
        if start is None:
            if p >= on:
                start, below = i, 0
        elif p < off:
            below += 1
            if below * FRAME_S >= min_silence_s:
                segs.append([start * FRAME_S, (i - below + 1) * FRAME_S])
                start = None
        else:
            below = 0
    if start is not None:
        segs.append([start * FRAME_S, len(probs) * FRAME_S])
    return [[round(max(0.0, s - pad_s), 3), round(e + pad_s, 3)] for s, e in segs]


def max_prob(probs: np.ndarray, t0: float, t1: float) -> float:
    a, b = max(0, int(t0 / FRAME_S)), min(len(probs), int(t1 / FRAME_S) + 1)
    return float(probs[a:b].max()) if b > a else 0.0
