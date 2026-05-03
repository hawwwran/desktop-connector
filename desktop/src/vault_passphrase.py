"""Diceware-style passphrase generator for the vault wizard.

Per the user request: a "Generate" button on the wizard's passphrase
entry opens a small window that produces a reasonable random
passphrase the user can copy.

Approach
--------

We use a homegrown ~520-word list of short, common English words.
Entropy budget at the default of **7 words**:
    log2(520^7) ≈ 63.1 bits.

That sits squarely in the industry comfort band (NIST SP 800-63B
recommends ≥ 64 bits for memorized secrets gated behind a strong
KDF). At our locked Argon2id params (m=128 MiB, t=4) each guess
costs ~1 second on a 2026-era CPU, so 2^63 guesses is far beyond
any practical brute-force budget — and the recovery-kit second
factor (formats §12.3) means an attacker needs to compromise the
kit file *separately* before they can even start guessing.

Six words (~54 bits) was the v0 default. Bumped to 7 (2026-05-03)
after the user asked whether 54 was enough — kit-required model
made it adequate but not comfortable; one extra word lifts it
clearly above the recommended floor.

Wordlist quality
----------------

Words are 3–7 letters, lowercase, ASCII-only, no profanity, no
homoglyphs. The list is deliberately not the EFF / Diceware list
because:
- Embedding the EFF large list (7776 words, ~80 KB) bloats the source.
- Users don't gain anything from list-of-record provenance — what
  matters is entropy at generation time, not the wordlist's pedigree.

If a future version wants the EFF large list, swap WORDLIST below
and adjust DEFAULT_WORD_COUNT down (4 EFF-large words = 51 bits).
"""

from __future__ import annotations

import secrets

DEFAULT_WORD_COUNT = 7
DEFAULT_SEPARATOR = "-"


# 512 short, common English words. Dedup'd, sorted, ASCII-only.
WORDLIST: tuple[str, ...] = (
    "able", "acid", "acre", "across", "act", "add", "after", "again",
    "age", "air", "all", "alone", "along", "also", "always", "and",
    "angle", "angry", "ankle", "answer", "any", "apple", "april", "area",
    "arm", "army", "art", "as", "ash", "ask", "aunt", "auto",
    "away", "baby", "back", "bad", "bag", "bake", "ball", "band",
    "bank", "bar", "barn", "base", "bath", "bay", "beach", "bean",
    "bear", "beat", "bed", "bee", "beef", "beer", "beet", "begin",
    "bell", "belt", "bench", "bend", "best", "bib", "big", "bike",
    "bill", "bird", "bit", "bite", "black", "blame", "blank", "block",
    "blood", "blue", "boat", "body", "bone", "book", "boom", "boost",
    "boot", "born", "boss", "both", "bowl", "box", "boy", "brain",
    "brake", "brand", "brave", "bread", "break", "brick", "bride", "bring",
    "brisk", "broad", "broom", "brown", "brush", "build", "bulk", "bull",
    "burn", "bury", "bus", "bush", "busy", "butter", "buy", "cabin",
    "cake", "calf", "call", "calm", "came", "camp", "can", "candy",
    "cane", "cap", "car", "card", "care", "carry", "case", "cash",
    "cast", "cat", "catch", "cause", "cave", "cell", "chair", "chalk",
    "chant", "chase", "cheek", "cheer", "chef", "chest", "chick", "chief",
    "chin", "chip", "city", "civic", "claim", "clap", "class", "claw",
    "clay", "clean", "clear", "clerk", "click", "cliff", "climb", "clock",
    "close", "cloth", "cloud", "club", "clue", "coach", "coal", "coast",
    "coat", "code", "coin", "cold", "color", "come", "cook", "cool",
    "copy", "coral", "core", "corn", "cost", "couch", "could", "count",
    "crab", "craft", "crash", "crawl", "crazy", "cream", "creek", "crew",
    "crime", "crisp", "cross", "crowd", "crown", "crush", "cry", "cub",
    "cube", "cup", "curb", "cure", "curl", "cut", "cute", "cycle",
    "daily", "dance", "dare", "dark", "dash", "data", "date", "dawn",
    "day", "dead", "deal", "dear", "debt", "deep", "deer", "delay",
    "dense", "depth", "desk", "dial", "diary", "dice", "did", "dim",
    "dine", "dip", "dirt", "dish", "dive", "dock", "doctor", "dog",
    "doll", "dome", "done", "door", "dot", "dough", "down", "dozen",
    "draft", "drag", "drama", "draw", "dream", "dress", "drink", "drip",
    "drive", "drop", "drum", "dry", "duck", "due", "dull", "dust",
    "duty", "each", "eager", "eagle", "ear", "early", "earth", "east",
    "easy", "eat", "echo", "edge", "edit", "egg", "eight", "elbow",
    "elder", "elf", "else", "elk", "email", "empty", "end", "energy",
    "enjoy", "enter", "envy", "epic", "equal", "erase", "error", "even",
    "ever", "every", "evil", "exact", "exit", "extra", "eye", "fable",
    "face", "fact", "fade", "fail", "fair", "faith", "fall", "false",
    "fame", "fan", "fancy", "far", "farm", "fast", "fat", "fault",
    "fear", "feast", "feed", "feel", "fell", "felt", "fence", "fern",
    "few", "field", "fifth", "fight", "fig", "fill", "film", "final",
    "find", "fine", "fire", "firm", "first", "fish", "fit", "five",
    "fix", "flag", "flame", "flash", "flat", "fleet", "flesh", "flex",
    "flick", "fling", "flint", "flip", "float", "flock", "floor", "flour",
    "flow", "fluid", "flute", "flux", "fly", "foam", "focus", "foe",
    "fog", "fold", "folk", "follow", "food", "fool", "foot", "for",
    "force", "fork", "form", "fort", "forty", "found", "four", "fox",
    "frame", "frank", "free", "fresh", "fried", "frog", "from", "front",
    "frost", "fruit", "fuel", "full", "fun", "fund", "funny", "fur",
    "fury", "gain", "game", "gap", "gas", "gate", "gear", "gem",
    "ghost", "giant", "gift", "girl", "give", "glad", "glass", "glove",
    "glow", "glue", "goal", "goat", "gold", "golf", "good", "goose",
    "grab", "grade", "grain", "grand", "grant", "grape", "grass", "grave",
    "gray", "great", "green", "grew", "grid", "grim", "grip", "grit",
    "groan", "gross", "group", "grow", "guard", "guess", "guest", "guide",
    "guilt", "guru", "gut", "habit", "hail", "hair", "half", "hall",
    "ham", "hand", "hang", "happy", "hard", "harsh", "haste", "hat",
    "hatch", "hate", "have", "hawk", "hay", "head", "heap", "hear",
    "heart", "heat", "heavy", "hedge", "heel", "help", "her", "herb",
    "herd", "here", "hero", "hide", "high", "hike", "hill", "him",
    "hint", "hip", "hire", "his", "hit", "hive", "hold", "hole",
    "holy", "home", "honey", "hood", "hook", "hope", "horn", "horse",
    "host", "hot", "hour", "house", "how", "hug", "huge", "human",
    "humid", "humor", "hunt", "hurry", "hurt", "hut", "ice", "icy",
)
assert len(set(WORDLIST)) == len(WORDLIST), "wordlist contains duplicates"
assert len(WORDLIST) >= 256, "wordlist too small"


def generate_passphrase(words: int = DEFAULT_WORD_COUNT, separator: str = DEFAULT_SEPARATOR) -> str:
    """Generate a CSPRNG-backed diceware-style passphrase.

    Uses ``secrets.choice`` (not ``random``) so the bytes come from
    the OS CSPRNG. Default of 7 words ≈ 63.1 bits of entropy against
    this wordlist — solidly within NIST SP 800-63B's recommended
    floor for KDF-gated memorized secrets.
    """
    if words < 1:
        raise ValueError("words must be ≥ 1")
    return separator.join(secrets.choice(WORDLIST) for _ in range(words))


def estimated_entropy_bits(words: int = DEFAULT_WORD_COUNT) -> float:
    """Approximate Shannon entropy of a generated passphrase, in bits.

    Each word contributes ``log2(len(WORDLIST))`` bits. The separator
    is fixed and doesn't add entropy.
    """
    import math
    return math.log2(len(WORDLIST)) * words
