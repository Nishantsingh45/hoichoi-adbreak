"""Tunable policy. Everything the business would want to change lives here, not in code paths."""
import os

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")

# Pacing rules (the "whether")
MAX_BREAKS_PER_HOUR = 6
MIN_GAP_BETWEEN_BREAKS_S = 240
MAX_AD_LOAD_PCT = 6.0           # total creative seconds / runtime
NO_BREAK_BEFORE_S = 120         # protect the cold open
NO_BREAK_IN_LAST_S = 60         # protect the ending

# Creative choice within a break
PREFERRED_CREATIVE_S = 20       # standard slot; 15 s when ad load is tight
LONG_CREATIVE_MIN_QUALITY = 0.85  # 30 s only at breaks this natural, and only with ad-load headroom
CREATIVE_BASE_URL = os.getenv("CREATIVE_BASE_URL", "/")  # prefix for catalogue creative urls in the manifest

# Cut-point rules (the "where"; hard, enforced in code)
BOUNDARY_SEARCH_WINDOW_S = 6.0  # how far from a Gemini scene boundary we search for a clean cut
MIN_SPEECH_CLEARANCE_S = 0.35   # no speech this close to the cut on either side
MIN_SOFT_GAP_S = 1.2            # a cut without a shot change needs at least this much silence
VAD_THRESHOLD = 0.4             # lower = more conservative (more audio counted as speech)
CUT_MAX_SPEECH_PROB = 0.15      # around the cut itself, even faint/music-masked speech is not tolerated

# Judgment policy
MIN_BREAK_QUALITY = 0.55
SAME_CONVERSATION_REJECT = 0.7
BRAND_BLOCK_THRESHOLD = 0.25    # fail-closed: even a 25% chance of a forbidden context blocks the brand
MIN_BRAND_FIT = 0.35            # on the 0-3 fit scale; below this no brand is relevant enough

# What happens to a break where no catalogue brand is safe and relevant:
#   "move"        - the break does not survive; the planner picks the next-best position (default)
#   "house_promo" - keep the break and fill it with the platform's own promo
UNSAFE_SLOT_POLICY = os.getenv("UNSAFE_SLOT_POLICY", "move")
HOUSE_PROMO = {
    "brand_id": "house_promo", "display_name": "House Promo", "category": "platform self-promotion",
    "target_contexts": [], "negative_contexts": [],
    "creatives": [{"id": "house_20s_bn", "duration_sec": 20, "language": "bn", "url": "ads/house_promo/house_20s_bn.mp4"}],
    "color": "#444B5A",
}
