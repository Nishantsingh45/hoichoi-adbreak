# Context-aware Ad Break Planner (hoichoi Hackathon '26, Problem 1)

Long-form Bengali drama in → semantically coherent scenes → frame-accurate, speech-free cut candidates →
AI-judged break quality → paced, brand-safe breaks with a catalogue creative each → **IAB VMAP 1.0 manifest**,
a debug JSON with every decision, and a player that actually cuts to the ad and resumes.

## The three questions the brief asks, and who answers them

| | Question | Answered by | How |
|---|---|---|---|
| **Where** | Is this a natural, non-jarring cut? | code + Gemini | Scene boundaries from Gemini are snapped to a real shot change inside a speech-free gap (Silero VAD, per-frame). Gemini then listens to a 10 s clip around each chosen cut; any dialogue → the break moves to the next clean cut or is dropped. |
| **Whether** | Is a break warranted, given pacing rules? | Jev + code | Jev scores each cut (how natural, same conversation?, emotional peak?). A planner picks the best-spaced set under max breaks/hour, min gap and max ad load, using the real creative durations (15/20/30 s). |
| **What** | Which brand's creative belongs here? | Jev + code | Two independent, fail-closed block layers: lexical evidence of a `negative_context` in the scene notes, and Jev's probability (≥ 0.25 blocks). Among unblocked brands, Jev's fit score for the dominant scene activity wins. A break with no safe, relevant brand does not survive; the planner re-plans. |

**Gemini perceives, Jev decides, code enforces.** Gemini (video-native) watches the episode once and writes
structured English scene notes (location, characters, dominant activity, mood, open-vocabulary tags, sensitive
themes). Jev (TypeSafe System One) never sees the video; it answers narrow typed questions over those notes
with calibrated probabilities, so thresholds mean something. Pacing rules, hard blocks and the manifest are code.

**Brands are data.** `brands/brands.json` is the official catalogue schema. A 9th brand pasted in the same schema
(UI or `POST /api/brands`) is judged from its own contexts and gets placeholder creatives at its catalogue paths;
drop real ad files at those paths and they are used untouched.

**Cost guard.** Every Gemini call is estimated first and refused past `GEMINI_BUDGET_USD`
(ledger in `out/gemini_ledger.json`). ~$0.10–0.20 per episode; Jev is $0.042 per million input tokens.

## Run locally

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt   # ffmpeg must be on PATH
cp .env.example .env                                                   # add GEMINI_API_KEY and TYPESAFE_API_KEY

python -m adbreak.pipeline data/videos/bhojon_bilashi.mp4
python -m adbreak.pipeline data/videos/bhojon_bilashi.mp4 --brands brands/brands.json brands/holdout_brand.json
uvicorn server:app --port 8000     # http://localhost:8000
```

Outputs per video in `out/<video_id>/`: `vmap.xml`, `result.json` (debug: scenes, every candidate with Jev's raw
answers, audits, rejection reasons, brand table per break), `analysis.json` (Gemini scene notes, cached).
Policy knobs live in `adbreak/config.py`.
