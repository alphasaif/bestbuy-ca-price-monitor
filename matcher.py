"""
matcher.py

Query construction and fuzzy match scoring for BestBuy Canada searches.

Grade hard-fail accepts either the parenthesized BestBuy condition label
("Refurbished (Excellent)") or a standalone token ("Excellent" / "Good")
with word boundaries. Upstream inventory grades are mapped to the matcher's
grade vocabulary via `dls_to_matcher_grade`.
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from fuzzywuzzy import fuzz

_log = logging.getLogger("bestbuy.matcher")


STOPWORDS = {
    "ram",
    "gb ram",
    "memory",
    "cellphone",
    "smartphone",
    "tablet",
}

ACCESSORY_PATTERNS = [
    "applecare", "protection plan", "warranty", "phone case", "ipad case",
    "tablet case", "protective case", "cover", "screen protector", "charger",
    "cable", "adapter", "mount", "holder", "stand", "dock", "stylus", "strap",
    "tempered glass", "film", "sleeve", "pouch", "skin", "monthly plan",
    "year plan", "insurance", "geek squad", "gift card", "replacement",
    "assembly", "compatible for", "compatible with", "service pack", "repair",
    "oem part", "hard buttons", "lcd display", "digitizer", "flex cable",
    "back glass", "battery door", "front glass", "rear camera", "front camera",
    # Added after the 2026-04 full-batch verifier caught a Galaxy S5 Neo
    # phone listing matched to a Battery Pack accessory listing.
    "battery pack", "earbuds", "headphones",
]

# Word-boundary single-word accessory keywords. Kept separate from the
# substring patterns above so a color name containing the substring doesn't
# false-positive (e.g. if a future phone ships in a color with "case" as a
# substring — none known today).
ACCESSORY_WORD_KEYWORDS = ["case"]

# Path fragments in the Best Buy Canada URL that indicate an accessory
# product category, even if the title doesn't trigger a keyword match.
ACCESSORY_URL_PATHS = [
    "/accessories/",
    "/cases-and-protection/",
    "/chargers/",
    "/cables/",
    "/audio-accessories/",
]

STRICT_MODEL_KEYWORDS = {
    "pro", "max", "plus", "ultra", "lite", "fe", "flip", "fold",
    "se", "mini", "air",
}

# Brand/series prefixes where a trailing small integer in the upstream model
# name represents the series/generation (e.g. "iPhone 11", "Galaxy S24",
# "Pixel 8", "Galaxy Z Fold 6") rather than a version-number suffix
# (e.g. "LG X Power 2", where 2 IS a version). The version-suffix check
# below skips models whose "<brand> <model>" matches any of these patterns.
VERSION_CHECK_SKIP_PATTERNS = [
    r"^apple\s+iphone",
    r"^samsung\s+galaxy\s+s\s*\d",       # Galaxy S24, S24+, S24 Ultra, S23 FE, ...
    r"^samsung\s+galaxy\s+note\s*\d",
    r"^samsung\s+galaxy\s+a\s*\d",
    r"^samsung\s+galaxy\s+m\s*\d",
    r"^samsung\s+galaxy\s+j\s*\d",
    r"^samsung\s+galaxy\s+z\s+fold",     # Galaxy Z Fold 6 — "6" is generation, not version
    r"^samsung\s+galaxy\s+z\s+flip",
    r"^google\s+pixel\s+\d",
    r"^oneplus\s+\d",
]


# --------------------------------------------------------------------------- #
# Upstream-grade → matcher-grade mapping. Hardcoded per project spec.
# Grades not in the table (DLS A, DLS B, DLS C, DLS C-) are filtered out
# upstream and never reach this matcher.
# --------------------------------------------------------------------------- #

DLS_TO_MATCHER_GRADE = {
    "DLS A+": "Excellent",
    "DLS A-": "Excellent",
    "DLS B+": "Good",
    "DLS B-": "Good",
}


def dls_to_matcher_grade(dls_grade: Optional[str]) -> str:
    """DLS grade -> matcher grade ('Excellent' | 'Good'). Returns '' if unmapped."""
    if not dls_grade:
        return ""
    return DLS_TO_MATCHER_GRADE.get(str(dls_grade).strip(), "")


@dataclass
class SearchTerms:
    brand: str
    model: str
    storage: Optional[str]
    color: Optional[str]
    device_type: Optional[str]

    @property
    def is_watch(self) -> bool:
        dt = (self.device_type or "").lower()
        ml = self.model.lower()
        return "watch" in dt or "watch" in ml

    def query(self) -> str:
        if self.is_watch:
            parts = [self.brand, self.model, "GPS"]
            return " ".join(p for p in parts if p).strip()
        parts = [self.brand, self.model]
        if self.storage:
            parts.append(_search_storage_form(self.storage))
        if self.color:
            parts.append(self.color)
        return " ".join(p for p in parts if p).strip()

    def query_without_color(self) -> str:
        parts = [self.brand, self.model]
        if self.is_watch:
            parts.append("GPS")
        elif self.storage:
            parts.append(_search_storage_form(self.storage))
        return " ".join(p for p in parts if p).strip()

    def query_without_storage(self) -> str:
        parts = [self.brand, self.model]
        if self.color:
            parts.append(self.color)
        return " ".join(p for p in parts if p).strip()

    def query_watch_unlocked(self) -> str:
        parts = [self.brand, self.model]
        return " ".join(p for p in parts if p).strip()


@dataclass
class Candidate:
    name: str
    price: Optional[float]
    url: str
    seller: Optional[str] = None


@dataclass
class MatchResult:
    candidate: Optional[Candidate]
    score: int
    is_approximate: bool
    reason: str


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #


def normalize_storage(raw: Optional[str]) -> Optional[str]:
    """
    Canonical storage form is ``<digits>GB``. ``1TB`` / ``2TB`` collapse to
    ``1024GB`` / ``2048GB`` so downstream compares don't trip over the
    upstream-vs-BestBuy wording split (upstream feed writes ``1024GB``,
    BestBuy product titles say ``1TB``).
    """
    if not raw:
        return None
    s = str(raw).strip().upper().replace(" ", "")
    m = re.match(r"^(\d+)(GB|TB)$", s)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        if unit == "TB":
            return f"{num * 1024}GB"
        return f"{num}GB"
    return s or None


def _canonicalize_storage_token(tok: str) -> str:
    """
    Canonicalise a storage token extracted from either side of a compare
    (upstream capacity vs BestBuy product title). Makes ``1tb`` and
    ``1024gb`` compare equal.
    """
    m = re.match(r"^(\d+)(gb|tb)$", (tok or "").lower())
    if not m:
        return (tok or "").lower()
    n, u = int(m.group(1)), m.group(2)
    return f"{n * 1024}gb" if u == "tb" else f"{n}gb"


def _search_storage_form(canonical: Optional[str]) -> Optional[str]:
    """
    Convert canonical ``1024GB``/``2048GB`` back to ``1TB``/``2TB`` for the
    SEARCH-QUERY STRING only. BB's product titles use the TB form, so a
    search for ``Samsung Galaxy S25 Ultra 1024GB ...`` buries 1TB listings
    while ``Samsung Galaxy S25 Ultra 1TB ...`` surfaces them on page 1.

    Internal storage comparisons (in score_candidate) keep using the
    canonical GB form and are unaffected. This helper is for query-build only.
    """
    if not canonical:
        return canonical
    m = re.match(r"^(\d+)GB$", canonical)
    if not m:
        return canonical
    n = int(m.group(1))
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}TB"
    return canonical


def normalize_brand(raw: Optional[str]) -> str:
    if not raw:
        return ""
    b = str(raw).strip()
    mapping = {
        "apple": "Apple", "samsung": "Samsung", "google": "Google",
        "motorola": "Motorola", "moto": "Motorola", "oneplus": "OnePlus",
        "lg": "LG", "sony": "Sony", "huawei": "Huawei", "nothing": "Nothing",
    }
    return mapping.get(b.lower(), b)


def normalize_model(raw: Optional[str]) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    for stop in STOPWORDS:
        s = re.sub(rf"\b{re.escape(stop)}\b", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def normalize_color(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    c = str(raw).strip()
    return c.title() if c else None


def condition_tokens(grade: str) -> List[str]:
    """
    Accepted condition tokens for a given matcher grade. The candidate name
    must contain at least one of these (case-insensitive, with word boundaries
    on the standalone form) or the candidate is hard-failed.
    """
    return {
        "Excellent": ["refurbished (excellent)", r"\bexcellent\b"],
        "Good":      ["refurbished (good)",      r"\bgood\b"],
        "Fair":      ["refurbished (fair)",      r"\bfair\b"],
        "Open Box":  ["open box"],
    }.get(grade, [])


def _name_matches_grade(cand_name: str, grade: str) -> bool:
    if not grade or grade not in ("Excellent", "Good", "Fair", "Open Box"):
        return True
    name_lower = (cand_name or "").lower()
    for tok in condition_tokens(grade):
        if tok.startswith(r"\b"):
            if re.search(tok, name_lower):
                return True
        else:
            if tok in name_lower:
                return True
    return False


def _clean_for_compare(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _extract_model_number(text: str) -> Optional[str]:
    text_lower = text.lower()
    m = re.search(r"iphone\s+(x|xs|xr|se)\b", text_lower)
    if m:
        return m.group(1)
    m = re.search(r"(iphone|pixel|moto\s*g?)\s*(\d+)", text_lower)
    if m:
        return m.group(2)
    m = re.search(r"galaxy\s*(s|a|z\s*(?:fold|flip))\s*(\d+)", text_lower)
    if m:
        return m.group(1).replace(" ", "") + m.group(2)
    m = re.search(r"(?:watch|series)\s*(\d+)", text_lower)
    if m:
        return m.group(1)
    m = re.search(r"ipad.*?(\d+)(?:th|st|nd|rd)", text_lower)
    if m:
        return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Build search terms from a listing row
# --------------------------------------------------------------------------- #


def build_search_terms(row: dict) -> SearchTerms:
    """
    Accepts either Google-Sheets-style keys (Brand/DEVICES/Storage/Color/Device Type)
    or Supabase-style keys (make/model/capacity/color/category). Falls through both.
    """
    brand = normalize_brand(row.get("make") or row.get("Brand"))
    model_raw = row.get("model") or row.get("DEVICES")
    model = normalize_model(model_raw)
    storage = normalize_storage(row.get("capacity") or row.get("Storage"))
    color = normalize_color(row.get("color") or row.get("Color"))
    device_type = (
        (row.get("category") or row.get("Device Type") or "").strip() or None
    )
    return SearchTerms(
        brand=brand, model=model, storage=storage,
        color=color, device_type=device_type,
    )


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _is_accessory(name: str, url: str = "") -> bool:
    nl = (name or "").lower()
    if any(pat in nl for pat in ACCESSORY_PATTERNS):
        return True
    for kw in ACCESSORY_WORD_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", nl):
            return True
    ul = (url or "").lower()
    if any(path in ul for path in ACCESSORY_URL_PATHS):
        return True
    return False


def _check_fan_edition_mismatch(model: str, candidate_name: str, candidate_url: str = "") -> bool:
    """
    Hard-fail when the upstream model is a Fan Edition ("Galaxy S23 FE" /
    "Galaxy S23 Fan Edition 5G") but the BB candidate is the regular
    non-FE model. Surfaced 2026-04 after listing 1362 (Galaxy S23 FE)
    matched plain "Galaxy S23".

    Triggers on the upstream side when the model contains "fan edition"
    (2-word phrase) OR the "fe" keyword appears as a whole word.
    When triggered, the BB candidate must contain "fan edition" or "fe"
    (whole-word) in either its title or URL, else hard-fail.
    """
    ml = (model or "").lower()
    upstream_is_fe = (
        "fan edition" in ml or bool(re.search(r"\bfe\b", ml))
    )
    if not upstream_is_fe:
        return False
    cand_hay = f"{(candidate_name or '').lower()} {(candidate_url or '').lower()}"
    cand_is_fe = (
        "fan edition" in cand_hay or bool(re.search(r"\bfe\b", cand_hay))
    )
    return not cand_is_fe


def _check_version_suffix_mismatch(
    brand: str, model: str, candidate_name: str
) -> bool:
    """
    Hard-fail when the upstream model name ends in what looks like a
    version/generation integer (e.g. "LG X Power 2", "Moto G 3") and the
    BB candidate's name doesn't contain that same integer as a standalone
    token.

    Skips well-known series brands where trailing numbers are part of the
    series identity (iPhone, Galaxy S/A/M/Note/J/Z Fold/Z Flip, Pixel,
    OnePlus) — see VERSION_CHECK_SKIP_PATTERNS.

    Surfaced 2026-04 after listing 1065 ("LG X Power 2") matched BB
    "LG X Power" (the 2016 predecessor).
    """
    brand_l = (brand or "").lower().strip()
    model_l = (model or "").lower().strip()
    brand_model = f"{brand_l} {model_l}".strip()
    if any(re.match(p, brand_model) for p in VERSION_CHECK_SKIP_PATTERNS):
        return False
    # Strip radio suffixes before looking at the trailing integer
    cleaned = re.sub(r"\s+(5g|lte|4g|gsm|wifi)\s*$", "", model_l).strip()
    tail = re.search(r"\s(\d+)\s*$", cleaned)
    if not tail:
        return False
    version_num = tail.group(1)
    # Skip obvious non-version numbers (storage-like or year-like)
    try:
        n = int(version_num)
    except ValueError:
        return False
    if n >= 16:
        # 16GB storage, year-like 2023, etc. — not a version suffix
        return False
    # Check the candidate name for the same integer as a standalone token
    # AFTER stripping storage (prevents "16GB" matching a "16" version).
    cand_stripped = re.sub(
        r"\b\d+\s*(gb|tb|mb)\b", "", (candidate_name or "").lower()
    )
    if re.search(rf"\b{re.escape(version_num)}\b", cand_stripped):
        return False
    return True


def _check_strict_keyword_mismatch(model: str, candidate_name: str) -> bool:
    # Pre-split fused digit suffixes on BOTH sides so a keyword like "flip"
    # is visible in "Galaxy Z Flip6" → "galaxy z flip 6" and the BB title's
    # "Flip6" gets the same treatment. Without this, either side asymmetric
    # (token-only model / substring candidate) trips on Galaxy Z Flip{5,6,7}
    # and Galaxy Z Fold{5,6,7} listings. See listings 1913, 1919, etc.
    _split_fused_digit = lambda s: re.sub(r"\b([a-z]+?)(\d+)\b", r"\1 \2", s)
    model_lower = _split_fused_digit(model.lower())
    name_lower = _split_fused_digit(candidate_name.lower())
    model_words = set(model_lower.split())

    FALSE_POSITIVE_CONTEXTS = {
        "pro": ["product", "processor", "professional", "production", "promo"],
        "mini": ["aluminium", "aluminum", "administration", "minimalist"],
        "plus": [],
        "air": ["repair", "fair", "chair"],
        "se": [],
    }

    for keyword in STRICT_MODEL_KEYWORDS:
        in_model = keyword in model_words
        in_name = False
        if keyword == "se":
            in_name = bool(re.search(r'\bse\b', name_lower))
        elif keyword == "plus":
            in_name = "plus" in name_lower or "+" in candidate_name
            if "+" in model:
                in_model = True
        elif keyword == "fe":
            # "Fan Edition" is the expanded form of "FE" — the upstream feed
            # writes "Galaxy S23 Fan Edition 5G" while Best Buy writes
            # "Galaxy S23 FE". Treat them as synonyms on both sides.
            in_model = (
                keyword in model_words
                or "fan edition" in model_lower
            )
            in_name = (
                bool(re.search(r"\bfe\b", name_lower))
                or "fan edition" in name_lower
            )
        else:
            # Word-boundary match on the candidate side too, so e.g.
            # "ultra" in STRICT_MODEL_KEYWORDS doesn't trip on colors like
            # "Ultramarine" (listing 705 regression).
            if re.search(rf"\b{re.escape(keyword)}\b", name_lower):
                fps = FALSE_POSITIVE_CONTEXTS.get(keyword, [])
                is_fp = any(fp in name_lower for fp in fps)
                in_name = not is_fp
        if in_model != in_name:
            return True
    return False


def _check_model_number_mismatch(model: str, candidate_name: str) -> bool:
    src_num = _extract_model_number(model)
    cand_num = _extract_model_number(candidate_name)
    if src_num and cand_num and src_num != cand_num:
        return True
    return False


def _check_device_type_mismatch(terms: SearchTerms, candidate_name: str) -> bool:
    name_lower = candidate_name.lower()
    if terms.is_watch:
        if "watch" not in name_lower:
            return True
        if any(w in name_lower for w in ("iphone", "galaxy s", "galaxy a", "pixel", "moto")):
            return True
    else:
        dt = (terms.device_type or "").lower()
        if "cellphone" in dt or "phone" in dt:
            if "watch" in name_lower and "iphone" not in name_lower:
                return True
    return False


def score_candidate(candidate: Candidate, terms: SearchTerms, grade: str = "") -> int:
    """Higher is better. Negative = mismatch."""
    if not candidate or not candidate.name:
        return -999

    # GRADE HARD FAIL — accept either "Refurbished (Excellent)" or
    # standalone "Excellent" with word boundary.
    if grade in ("Excellent", "Good", "Fair", "Open Box"):
        if not _name_matches_grade(candidate.name, grade):
            return -999

    name_lower = candidate.name.lower()
    name_clean = _clean_for_compare(candidate.name)
    score = 0

    if _is_accessory(candidate.name, candidate.url):
        _log.debug(
            "Rejected: candidate is an accessory (title or URL path match). "
            "title=%r url=%r",
            candidate.name,
            candidate.url,
        )
        return -500
    if _check_device_type_mismatch(terms, candidate.name):
        _log.debug(
            "Rejected: device-type mismatch. terms=%r name=%r",
            terms.device_type,
            candidate.name,
        )
        return -500
    if _check_model_number_mismatch(terms.model, candidate.name):
        _log.debug(
            "Rejected: model-number mismatch. model=%r name=%r",
            terms.model,
            candidate.name,
        )
        return -400
    if _check_fan_edition_mismatch(terms.model, candidate.name, candidate.url):
        _log.debug(
            "Rejected: upstream model specifies Fan Edition but BB title/URL "
            "lacks FE / Fan Edition. model=%r name=%r",
            terms.model,
            candidate.name,
        )
        return -350
    if _check_version_suffix_mismatch(terms.brand, terms.model, candidate.name):
        _log.debug(
            "Rejected: upstream model has version-number suffix missing from "
            "BB title. brand=%r model=%r name=%r",
            terms.brand,
            terms.model,
            candidate.name,
        )
        return -325
    if _check_strict_keyword_mismatch(terms.model, candidate.name):
        _log.debug(
            "Rejected: strict-keyword (Pro/Max/Plus/etc.) mismatch. "
            "model=%r name=%r",
            terms.model,
            candidate.name,
        )
        return -300

    if terms.storage and not terms.is_watch:
        storage_clean = _clean_for_compare(terms.storage)
        if storage_clean in name_clean:
            score += 5
        else:
            other_storage = re.search(r"(\d+)(gb|tb)", name_lower)
            if other_storage:
                expected = re.search(r"(\d+)(gb|tb)", terms.storage.lower())
                if expected and (
                    _canonicalize_storage_token(other_storage.group(0))
                    != _canonicalize_storage_token(expected.group(0))
                ):
                    # 1TB / 1024GB are equivalent after canonicalisation.
                    return -500

    if terms.brand and _clean_for_compare(terms.brand) in name_clean:
        score += 3
    if terms.model:
        for word in terms.model.split():
            if len(word) < 2:
                continue
            if _clean_for_compare(word) in name_clean:
                score += 3
    if terms.color and _clean_for_compare(terms.color) in name_clean:
        score += 2
    if candidate.price is not None and candidate.price > 20:
        score += 2
    if "unlocked" in name_lower or "gps" in name_lower:
        score += 2
    if terms.is_watch:
        if "watch" in name_lower:
            score += 3
    elif terms.device_type:
        dt = terms.device_type.lower()
        if "cellphone" in dt or "phone" in dt:
            if any(w in name_lower for w in ("iphone", "galaxy", "pixel", "moto", "phone")):
                score += 3

    fuzzy = fuzz.token_set_ratio(terms.query(), candidate.name) if candidate.name else 0
    score += fuzzy // 20

    return score


def pick_best_match(candidates: List[Candidate], terms: SearchTerms, grade: str = "") -> MatchResult:
    if not candidates:
        return MatchResult(None, 0, False, "No search results")

    non_accessories = [c for c in candidates if not _is_accessory(c.name, c.url)]
    pool = non_accessories if non_accessories else candidates

    scored: List[Tuple[int, Candidate]] = [(score_candidate(c, terms, grade), c) for c in pool]
    scored.sort(key=lambda t: t[0], reverse=True)

    best_score, best = scored[0]

    if best_score < 5:
        return MatchResult(None, best_score, False,
                           f"No confident match (best score {best_score})")

    is_approx = False
    reasons = []
    name_clean = _clean_for_compare(best.name)
    if terms.storage and not terms.is_watch and _clean_for_compare(terms.storage) not in name_clean:
        is_approx = True
        reasons.append("storage not in listing name")
    if terms.color and _clean_for_compare(terms.color) not in name_clean:
        if best_score < 10:
            is_approx = True
            reasons.append("color mismatch")

    reason = "Exact match" if not is_approx else "Approximate match: " + ", ".join(reasons)
    return MatchResult(best, best_score, is_approx, reason)


def pick_lowest_price(candidate: Candidate, marketplace_prices: List[float]) -> float:
    prices = []
    if candidate.price is not None and candidate.price > 0:
        prices.append(candidate.price)
    for p in marketplace_prices:
        if p is not None and p > 0:
            prices.append(p)
    if not prices:
        return 0.0
    return min(prices)


def infer_bb_grade_from_name(name: str) -> Optional[str]:
    """
    Infer the bestbuy_grade enum value ('Excellent'|'Good'|'Fair') from a
    product name. Returns None if no refurbished/condition marker present.
    """
    if not name:
        return None
    nl = name.lower()
    if "excellent" in nl:
        return "Excellent"
    if re.search(r"\bgood\b", nl):
        return "Good"
    if re.search(r"\bfair\b", nl):
        return "Fair"
    return None
