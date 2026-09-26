"""Audit: before a break is committed, Gemini listens to a 10-second clip centred on the cut and confirms
nobody is mid-sentence. This backs up the local speech detector where music may mask dialogue."""
import tempfile
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from . import budget, config
from .media import run, src

HALF_WINDOW_S = 5.0
_client = None


class CutAudit(BaseModel):
    speech_near_cut: bool = Field(description="Anyone speaking, or a word being cut off, between 00:04 and 00:06")
    sentence_spans_cut: bool = Field(description="A line of dialogue starts before 00:05 and continues after it")
    what_is_heard: str = Field(description="Short English description of the audio around 00:05")


PROMPT = """This 10-second clip from a Bengali drama is centred on a planned TV ad-break cut at exactly 00:05.
Listen carefully to the audio around 00:05. Report whether anyone is speaking (including a word being cut
off) between 00:04 and 00:06, and whether a line of dialogue starts before 00:05 and continues after it.
Background music, ambience and sound effects do not count as speech."""


def audit_cut(video, t: float) -> dict:
    """`video` is a local Path or a remote URL; for a URL ffmpeg fetches only the bytes of this 10 s window."""
    label = f"audit {Path(str(video).split('?')[0]).name}@{t:.1f}"
    budget.check(label, budget.estimate(config.GEMINI_MODEL, 2 * HALF_WINDOW_S, 300))
    start = max(0.0, t - HALF_WINDOW_S)
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src(video), "-t", str(2 * HALF_WINDOW_S),
             "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
             "-c:a", "aac", "-b:a", "96k", str(clip)])
        data = clip.read_bytes()
    global _client
    _client = _client or genai.Client()  # keep a reference: a temporary Client closes its HTTP session on GC
    resp = _client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=data, mime_type="video/mp4"), PROMPT],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=CutAudit,
                                           media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                                           thinking_config=types.ThinkingConfig(thinking_budget=0),
                                           temperature=0.0),
    )
    budget.record(label, config.GEMINI_MODEL, resp.usage_metadata)
    audit: CutAudit = resp.parsed
    if audit is None:
        # Fail closed: an unreadable audit counts as a failed audit.
        return {"passed": False, "what_is_heard": "audit unavailable", "speech_near_cut": None, "sentence_spans_cut": None}
    return {**audit.model_dump(), "passed": not (audit.speech_near_cut or audit.sentence_spans_cut)}
