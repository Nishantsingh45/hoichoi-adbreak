"""Perception: Gemini watches the episode once and returns structured, English scene notes."""
import time
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from . import budget, config

MAX_OUTPUT = 24_000
THINKING = 2_048

SENSITIVE_THEMES = [
    "death", "funeral_or_mourning", "illness_or_hospital", "injury_or_blood", "violence_or_fight",
    "crime_theft_or_fraud", "kidnapping", "accident_or_crash", "natural_disaster", "alcohol", "smoking",
    "romance_or_intimacy", "horror_or_fear", "poverty_or_financial_distress", "vomiting_or_disgust",
    "religious_ritual",
]


class Scene(BaseModel):
    start: str = Field(description="Scene start as MM:SS")
    end: str = Field(description="Scene end as MM:SS")
    location: str
    characters: list[str] = Field(description="Who is on screen, by name if known, else a short description")
    dominant_activity: str = Field(description="The main thing happening on screen, e.g. 'family eating dinner'")
    mood: str
    summary: str = Field(description="2-3 sentences in English, including what the dialogue is about")
    opening_gist: str = Field(description="What is happening / being said in the first few seconds")
    closing_gist: str = Field(description="What is happening / being said in the last few seconds")
    ends_on: str = Field(description="One of: resolved, dramatic_beat, cliffhanger, mid_conversation, transition")
    tags: list[str] = Field(description="5-15 short lowercase English labels covering every notable setting, activity, "
                                        "object and theme in the scene, e.g. kitchen, eating, bathroom, hospital, "
                                        "phone call, driving, crying, wedding, funeral, smoking, alcohol, money, shopping")
    sensitive_themes: list[str] = Field(description="Subset of the allowed sensitive theme labels; empty if none")


class EpisodeAnalysis(BaseModel):
    synopsis: str
    scenes: list[Scene]


PROMPT = f"""You are a senior broadcast editor at a Bengali OTT platform. Watch and listen to this entire
Bengali drama episode and segment it into semantically coherent SCENES.

A scene is continuous action in one location and time with the same dramatic situation. Start a new scene
only when the location, time, or dramatic situation changes. Never split one ongoing conversation into two
scenes. Most scenes last 30 seconds to 4 minutes. Cover the full runtime from 00:00 to the very end with no
gaps or overlaps; each scene's start equals the previous scene's end. Treat title cards and credits as scenes.

Timestamps must be precise MM:SS, taken from where the picture actually changes.
Write every description in English (translate and summarise the Bengali dialogue).
Tags are used for advertising brand-safety checks, so be exhaustive about settings, activities and themes,
including brief or background ones (someone eating in the corner, a cigarette, a hospital corridor).
For sensitive_themes use only these labels, and include a label whenever the theme is present or strongly
implied, even briefly: {", ".join(SENSITIVE_THEMES)}."""


def _client() -> genai.Client:
    return genai.Client()


def analyse(video: Path, duration_s: float, log=print) -> dict:
    if duration_s > budget.max_video_minutes() * 60:
        raise budget.BudgetExceeded(f"{video.name} is {duration_s / 60:.0f} min; limit is "
                                    f"{budget.max_video_minutes():.0f} min (GEMINI_MAX_VIDEO_MINUTES)")
    budget.check(f"analyse {video.name}", budget.estimate(config.GEMINI_MODEL, duration_s, MAX_OUTPUT + THINKING))
    client = _client()
    t0 = time.time()
    log(f"  uploading {video.name} ({video.stat().st_size / 1e6:.1f} MB) to Gemini...")
    uploaded = client.files.upload(file=str(video))
    log(f"  uploaded in {time.time() - t0:.0f}s; waiting for Gemini to process the video...")
    while uploaded.state.name == "PROCESSING":
        time.sleep(4)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini could not process {video.name}: {uploaded.state}")
    log(f"  ready after {time.time() - t0:.0f}s; generating scene notes...")

    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[uploaded, PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EpisodeAnalysis,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            max_output_tokens=MAX_OUTPUT,
            thinking_config=types.ThinkingConfig(thinking_budget=THINKING),
            temperature=0.2,
        ),
    )
    budget.record(f"analyse {video.name}", config.GEMINI_MODEL, resp.usage_metadata)
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass
    analysis: EpisodeAnalysis = resp.parsed
    if analysis is None:
        raise RuntimeError(f"Gemini returned unparseable output: {resp.text[:500]}")
    u = resp.usage_metadata
    return {
        **analysis.model_dump(),
        "model": config.GEMINI_MODEL,
        "usage": {
            "prompt_tokens": u.prompt_token_count,
            "output_tokens": u.candidates_token_count,
            "thinking_tokens": getattr(u, "thoughts_token_count", None),
        },
    }


def to_seconds(ts: str) -> float:
    parts = [float(p) for p in ts.strip().split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs
