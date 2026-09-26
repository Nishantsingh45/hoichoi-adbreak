"""Judgment: Jev (TypeSafe System One) turns scene notes into calibrated, typed decisions.

Jev never sees the video; it judges Gemini's English scene notes plus the audio facts measured in code.
Raw probabilities are kept in the debug output so policy thresholds can be tuned without re-running inference.
"""
import asyncio

from typesafe_sdk import AsyncTypeSafeClient, Noul, Score

from . import brands as brand_lib
from . import config


def _scene_view(scene: dict, edge: str) -> dict:
    view = {k: scene[k] for k in ("location", "characters", "dominant_activity", "mood", "summary", "sensitive_themes")}
    view["tags"] = scene.get("tags", [])
    if edge == "before":
        view["how_it_ends"] = scene["closing_gist"]
        view["ending_type"] = scene["ends_on"]
    else:
        view["how_it_begins"] = scene["opening_gist"]
    return view


def _break_state(c: dict, synopsis: str) -> dict:
    return {
        "episode_synopsis": synopsis,
        "scene_before_cut": _scene_view(c["scene_before"], "before"),
        "scene_after_cut": _scene_view(c["scene_after"], "after"),
        "cut_point_audio": {
            "seconds_of_silence_before_cut": c["silence_before_s"],
            "seconds_of_silence_after_cut": c["silence_after_s"],
            "visual_shot_change_at_cut": c["kind"] == "shot_cut",
        },
    }


BREAK_QUESTIONS = {
    "natural_break": Score(
        instructions="A streaming service wants to insert a TV commercial break exactly at this cut between two "
                     "scenes of a Bengali drama. How natural would a commercial interruption feel to a viewer here?",
        criteria=[
            "Jarring: the same conversation, argument or action is clearly still going on across the cut.",
            "Abrupt: the scene changes, but the story is mid-beat; the next scene directly continues the same "
            "tense exchange or an unresolved reveal.",
            "Acceptable: one scene wraps up and a different scene begins, with some emotional carry-over.",
            "Ideal: the scene before has resolved or landed on a deliberate dramatic beat, and the next scene starts "
            "somewhere new or later in time, like a classic TV act break.",
        ],
    ),
    "same_conversation": Noul(
        instructions="Does the same conversation or continuous action carry on across this cut, with the scene "
                     "after being a direct continuation of the scene before?",
        criteria={"true": "The same people continue the same exchange or action right after the cut.",
                  "false": "The scene after is a different moment, place, time or situation."},
    ),
    "emotional_peak": Noul(
        instructions="Is the scene before the cut ending at an intense emotional peak (grief, sobbing, confrontation, "
                     "shock) where cutting straight to a commercial would feel insensitive?",
        criteria={"true": "The ending is emotionally intense and raw.",
                  "false": "The ending is calm, light, resolved or only mildly emotional."},
    ),
}


def break_quality(answers) -> float:
    natural = answers.scores["natural_break"].score / 3
    same = answers.nouls["same_conversation"].noul
    peak = answers.nouls["emotional_peak"].noul
    return round(0.55 * natural + 0.25 * (1 - same) + 0.20 * (1 - peak), 4)


def _match_state(c: dict) -> dict:
    """Scene-local evidence only: no episode synopsis (genre and backstory must not colour the check), and the
    scene after the break counts only by how it opens."""
    after = c["scene_after"]
    return {
        "scene_before_break": _scene_view(c["scene_before"], "before"),
        "opening_of_scene_after_break": {
            "location": after["location"], "dominant_activity": after["dominant_activity"],
            "first_moments": after["opening_gist"],
        },
    }


FLOOR_QUESTION = Noul(
    instructions="Platform brand-safety floor: does the scene before the break, or the opening of the scene after "
                 "it, show or explicitly discuss sexual content (sex work, nudity, sexual assault) or illegal drug "
                 "use or dealing?",
    criteria={"true": "Sexual content or illegal drugs are shown or explicitly discussed.",
              "false": "Neither appears; romance, flirting, alcohol or smoking alone do not count."},
)


def _brand_questions(brands: list[dict]) -> dict:
    qs = {"floor": FLOOR_QUESTION}
    for b in brands:
        brand = {"name": b["name"], "category": b["category"]}
        qs[f"block__{b['id']}"] = Noul(
            instructions={
                "task": "Brand-safety check before placing this brand's TV ad in the break between the two scenes.",
                "brand": brand,
                "forbidden_contexts": b["negative_contexts"],
                "question": "Is any of the forbidden contexts actually shown on screen or explicitly talked about "
                            "in the scene before the break, or in the opening of the scene after it?",
            },
            criteria={"true": "A forbidden context is shown or explicitly discussed in the scene before the break "
                              "or the opening of the scene after it, even briefly.",
                      "false": "None of the forbidden contexts is shown or referred to there; the genre, the "
                               "episode's wider story, or things that happen later do not count."},
        )
        qs[f"fit__{b['id']}"] = Score(
            instructions={
                "task": "How well does this brand's ad match what is happening in the scene just before the break? "
                        "The dominant activity of the scene matters most.",
                "brand": {**brand, "scenes_the_brand_wants": b["target_contexts"]},
            },
            criteria=[
                "Unrelated: nothing in the scene connects to the brand's category or use.",
                "Loose: suits the general mood or audience, but not the activity on screen.",
                "Related: the setting or a secondary activity naturally connects to the brand.",
                "Strong: the dominant activity on screen is exactly what the brand is about.",
            ],
        )
    return qs


async def _ask(client, sem, state, questions):
    async with sem:
        return await client.system_one(state, questions, model=config.JEV_MODEL)


async def _judge_breaks(candidates, synopsis):
    sem = asyncio.Semaphore(16)
    async with AsyncTypeSafeClient() as client:
        return await asyncio.gather(*[_ask(client, sem, _break_state(c, synopsis), BREAK_QUESTIONS) for c in candidates])


def judge_breaks(candidates: list[dict], synopsis: str) -> None:
    """Annotates each candidate in place with Jev's raw answers and a composite quality."""
    results = asyncio.run(_judge_breaks(candidates, synopsis))
    for c, r in zip(candidates, results):
        c["jev"] = {
            "natural_break": round(r.scores["natural_break"].score, 3),
            "natural_break_confidence": round(r.scores["natural_break"].confidence, 3),
            "same_conversation": round(r.nouls["same_conversation"].noul, 3),
            "emotional_peak": round(r.nouls["emotional_peak"].noul, 3),
            "input_tokens": r.usage.input_tokens,
        }
        c["quality"] = break_quality(r)


async def _match(c, brands):
    async with AsyncTypeSafeClient() as client:
        return await client.system_one(_match_state(c), _brand_questions(brands), model=config.JEV_MODEL)


def match_brand(c: dict, brands: list[dict], synopsis: str = "") -> dict:
    """The "what": three independent, fail-closed block layers (platform floor; lexical evidence of a brand's
    negative contexts in the scene notes; Jev's probability), then Jev's fit score picks the best unblocked brand."""
    r = asyncio.run(_match(c, brands))
    before, after = c["scene_before"], c["scene_after"]
    floor_lex = brand_lib.floor_blocks(before, after)
    floor_p = r.nouls["floor"].noul
    floor = floor_lex + ([f"jev {floor_p:.2f}"] if floor_p >= config.BRAND_BLOCK_THRESHOLD else [])
    table = []
    for b in brands:
        p_block = r.nouls[f"block__{b['id']}"].noul
        fit = r.scores[f"fit__{b['id']}"].score
        lexical = brand_lib.lexical_blocks(b, before, after)
        blocked_by = ((["platform_floor"] if floor else []) + (["lexical"] if lexical else [])
                      + (["jev"] if p_block >= config.BRAND_BLOCK_THRESHOLD else []))
        table.append({
            "brand_id": b["id"], "brand": b["name"],
            "p_forbidden_context": round(p_block, 3),
            "fit": round(fit, 3),
            "blocked": bool(blocked_by),
            "blocked_by": blocked_by,
            "lexical_evidence": lexical,
        })
    table.sort(key=lambda row: (row["blocked"], -row["fit"]))
    allowed = [row for row in table if not row["blocked"] and row["fit"] >= config.MIN_BRAND_FIT]
    return {"choice": allowed[0] if allowed else None, "table": table, "floor": floor,
            "input_tokens": r.usage.input_tokens}
