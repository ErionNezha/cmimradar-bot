#!/usr/bin/env python3
"""RADAR shared logic — single source of truth for the bot (and the pilot).

Port of the canonical rules in ~/workspace/radar-verify/bot-sync-spec.md §2
and ~/workspace/radar-verify/pyet_radarin_reference.py (that file IS the spec
for search / "Pyet Radarin" AI).

Stdlib only: json, re, statistics, unicodedata, difflib, datetime.
The bot imports this as a sibling module (same dir — no sys.path hack).
The pilot imports it via sys.path (see spec §6).

Prefer scanner fields when present (group["best"] / ["best_in_stock"] /
["verdict"] — written by build.py + buy_signal.py after the next scan) and
fall back to the pure computation here; both paths must give the same answer.
The scanner field is "best", NOT "cheapest": the bot used to read
group["cheapest"], which the scanner never writes, so the preference path
never fired (D4, fixed 2026-11-19). "best" is trusted ONLY when the group
also carries "best_in_stock" — that key is the marker Part A redefined
"best" to cheapest_valid; an old "best" without "best_in_stock" must NEVER
be trusted (it may be an out-of-stock offer, e.g. REDMI 22.990 L).
See scanner_cheapest() / scanner_verdict() below; cheapest_valid(),
cheapest_in_stock() and canonical_cheapest() stay PURE computation
(the fallback), so harness comparisons scanner==pure keep working.

Documented approximations / deviations (do not "fix" silently):
- Fuzzy match uses difflib.SequenceMatcher ratio >= 0.92 as the Python
  approximation of the website's Levenshtein <=1/<=2 (spec §2.4).
- merge_groups() uses the WEBSITE's merge-key normalisation (lowercase,
  e->e, c->c, NFD accent strip, NO punctuation removal, NO space collapsing)
  because that is what produces the specified merged count 19.497 -> 19.370.
  The reference norm() would over-merge to 19.359 (it also merges titles
  that differ only by punctuation, e.g. "Nintendo Switch - OLED" with
  "Nintendo Switch Oled"). See _merge_key_norm().
- verdict_for(history, current=None): the spec §6 signature is
  verdict_for(history); the optional `current` lets the caller pass the
  group's cheapest VALID price (spec §2.3: the verdict rests on the
  cheapest valid price, not on the last history point). With current=None
  it uses the last history price, exactly as the website does when the
  history's last point IS the current price.
- answer_ai() applies the laptop-spec rule to the PICKS as well as to the
  honest fallback (spec §2.5 says explicitly "applies to the picks AND to
  the honest fallback"; the reference file applies it only to the fallback).
- is_used() also matches "second hand", "open box" and "ish ekspozite"
  (spec §2.1 — the reference file checks only U- / USED / I PERDORUR /
  REFURBISHED / I RINOVUAR).
"""

import json  # noqa: F401  (kept: part of the spec §6 stdlib-only import list)
import re
import statistics
import unicodedata
from difflib import SequenceMatcher
from datetime import date  # noqa: F401  (kept: part of the spec §6 import list)

SITE_URL = "https://cmimradar.netlify.app"
STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED = "në pritje", "miratuar", "refuzuar"


# ---------------------------------------------------------------- norm / synonyms
def norm(s):
    """lowercase, ë→e, ç→c, NFD accent strip, non-alphanumeric → space, collapse."""
    s = (s or "").lower().replace("ë", "e").replace("ç", "c")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


SYN = {
    "ajfon": {"iphone"}, "iphone": {"iphone"},
    "celular": {"telefon", "celular"}, "celulari": {"telefon", "celular"},
    "telefon": {"telefon", "celular"}, "telefoni": {"telefon", "celular"},
    "kufje": {"kufje", "headphones", "earphones", "earbuds", "airpods"},
    "kufjet": {"kufje", "headphones", "earphones", "earbuds", "airpods"},
    # D10 (2026-11-19): EN keys = 'kufje' set (mirrors site round-2 fix (a));
    # without them an EN query "headphones" never matches an "EARBUDS ..." title.
    "headphones": {"kufje", "headphones", "earphones", "earbuds", "airpods"},
    "earphones": {"kufje", "headphones", "earphones", "earbuds", "airpods"},
    "earbuds": {"kufje", "headphones", "earphones", "earbuds", "airpods"},
    "airpods": {"kufje", "headphones", "earphones", "earbuds", "airpods"},
    "fshese": {"aspirator", "fshese", "vacuum"},
    "fshesa": {"aspirator", "fshese", "vacuum"},
    "aspirator": {"aspirator", "vacuum"},
    "laptop": {"laptop"}, "laptopi": {"laptop"},
    "tv": {"tv", "televizor"}, "televizor": {"tv", "televizor"},
    "ps5": {"ps5", "playstation"}, "playstation": {"playstation"},
}

STOPWORDS = {
    "me", "e", "te", "ne", "dhe", "per", "i", "a", "nen", "deri", "mire",
    "lire", "liri", "ku", "eshte", "student", "studenta", "studente",
    "studenti", "studentet", "pa", "kabllo", "wireless", "nese", "cili",
    "cila", "cheapest", "under", "best", "good", "lira", "l", "lek", "leke",
    # Faza 1 (2026-11-20, audit §13.3/#8): fjalët-pyetje nuk janë terma
    # kërkimi — "sa kushton iPhone 17?" kërkon "iphone 17", jo "sa/kushton".
    # Vetëm fjalë-pyetje; fjalët kohore/njoftuese (muajin/kaluar/njofto/bie)
    # NUK hiqen këtu — ato i përkasin intent-eve të Fazës 3 (jo në scope).
    "sa", "kushton", "kushtonte", "cilin", "cilen", "pse", "si", "cfare",
}
# Back-compat alias (the reference file calls this STOP).
STOP = STOPWORDS

IGNORE = {"inch", "inc", "korrent", "me", "e"}

CATMAP = {  # query keyword -> category tokens that mark the right device class
    "iphone": {"telefon", "celular", "smartphone"},
    "ajfon": {"telefon", "celular", "smartphone"},
    "telefon": {"telefon", "celular", "smartphone"},
    "celular": {"telefon", "celular", "smartphone"},
    "laptop": {"laptop"},
    "tv": {"tv", "televizor"}, "televizor": {"tv", "televizor"},
    "ps5": {"console", "playstation", "konsole"},
    "playstation": {"console", "playstation", "konsole"},
    "kufje": {"kufje", "audio", "headphones", "earphones"},
    "kufjet": {"kufje", "audio", "headphones"},
    # D10 (2026-11-19): EN keys = 'kufje' set (mirrors site round-2 fix (a)).
    "headphones": {"kufje", "audio", "headphones", "earphones"},
    "earphones": {"kufje", "audio", "headphones", "earphones"},
    "earbuds": {"kufje", "audio", "headphones", "earphones"},
    "airpods": {"kufje", "audio", "headphones", "earphones"},
    "fshese": {"aspirator", "fshese", "vacuum"},
    "fshesa": {"aspirator", "fshese", "vacuum"},
    "aspirator": {"aspirator", "vacuum"},
    "samsung": {"telefon", "celular", "smartphone", "tv", "televizor",
                "smartwatch", "tablet"},
}

ACCESSORY_MARKERS = {
    # reference set (pyet_radarin_reference.py), verbatim
    "mount", "bracket", "mbajtese", "mbajtes", "kase", "mbules", "mbrojt",
    "folie", "xham", "kabel", "karikues", "adapter", "kit", "filter", "filtr",
    "kapak", "mbështjell", "mask", "lens", "thjerrez", "kuti", "shtrese",
    "rrip", "dock", "stend", "futroll", "kellef", "kelle", "sticker", "cord",
    "cante", "stativ", "stand", "mouse", "kabllo", "ftohes", "adaptues",
    "backpack", "tastier", "mousepad", "shpinore", "bateri", "kufje",
    "mikrofon", "webcam", "kamer", "hub", "mbeshtjell", "educational",
    "barbie", "toy", "loder", "pjese",
    # extra markers from spec §2.5 (live AI failures proved them necessary).
    # Stored normed (ë→e, ç→c, accent-stripped) because matching runs on
    # norm()ed title tokens with startswith().
    "kollja", "aromatizues", "peceta", "ngjites", "kornize", "fleks",
    "porte", "safeguard",
}

DEVICE_CORES = {"tv", "televizor", "laptop", "telefon", "celular", "iphone",
                "ajfon", "ps5", "playstation", "samsung", "fshese", "fshesa",
                "aspirator"}

LAPTOP_SPEC = {"ram", "intel", "amd", "core", "celeron", "ryzen", "ssd",
               "hdd", "emmc", "ghz", "kryo", "snapdragon", "mediatek",
               "n4020", "n100", "i3", "i5", "i7", "i9"}

SMARTPHONE_WORDS = {"galaxy", "redmi", "iphone", "pixel", "poco", "honor",
                    "motorola", "realme", "oneplus", "nothing", "infinix",
                    "tecno", "vivo", "oppo", "nokia", "xiaomi", "samsung",
                    "huawei", "itel", "tcl", "zte", "alcatel"}


# Faza 1 (2026-11-20, audit §13.3/#1c): trajta e shquar → trajta e
# pashquar, para TË GJITHA kontrolleve (deviceQuery, CATMAP, "laptop" in
# core, laptop-spec). "laptopi" s'është në DEVICE_CORES/CATMAP — pa këtë
# normalizim filtri i pajisjes fiket dhe aksesorët kalojnë si pajisje.
CORE_CANON = {"laptopi": "laptop", "telefoni": "telefon",
              "celulari": "celular", "kufjet": "kufje",
              "frigoriferi": "frigorifer", "televizori": "televizor",
              "fshesa": "fshese"}


# ---------------------------------------------------------------- budget
def eur_amount(raw):
    """Shuma në EUR nga pyetja ("800€" / "800 euro" / "1.200€"), ose None.

    Kujdes: `\\b` pas `€` dështon gjithmonë ( € s'është \\w) — prandaj
    simboli s'ka kufi fjale, ndërsa "euro"/"eur" e kanë.
    """
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?:€|euros?\b|eur\b)",
                  raw or "", re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1).replace(".", "").replace(",", ""))


def parse_budget(raw, fx=None):
    """Parse budget from the RAW question (before norm strips '.'/',' separators).

    EUR ("800€" / "800 euro") konvertohet me `fx` (kursi i skanimit,
    report.json: fx.EUR_ALL). Pa kurs (fx None/<=0) kthehet None — kurrë
    nuk shpiket kurs; numri hiqet prapë nga tokenët (strip_budget_tokens)
    që "800" të mos bëhet token-modeli. `fx` merret nga bot_worker (_fx()).
    """
    raw = raw or ""
    # EUR — PARA çdo dege tjetër ("1.200€" s'duhet të lexohet si 1.200 L).
    eur = eur_amount(raw)
    if eur is not None:
        if fx and fx > 0:
            # int(x + 0.5) = Math.round i site-it (JS) për numra pozitivë.
            return int(eur * fx + 0.5)
        return None
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})+)\s*(mij[eë]|mije|k\b)?", raw)
    if m:
        return int(m.group(1).replace(".", "").replace(",", ""))
    m = re.search(r"(\d+)\s*(mij[eë]|mije|k\b)", raw, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000
    # "800 lek" — 1-3 shifra + fjalë valute (pas degës së mijësheve, që
    # "10.000 lek" të kapet si 10.000, jo si "000").
    m = re.search(r"\b(\d{1,3})\s*(?:lek[e]?)\b", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{4,6})\b", raw)
    if m and ("lek" in raw.lower() or "deri" in raw.lower()
              or "nën" in raw.lower() or "nen" in raw.lower()
              or "under" in raw.lower()):
        return int(m.group(1))
    return None


def strip_budget_tokens(raw, budget):
    """Remove the budget expression from the RAW question, then normalize.

    Hiqen edhe shumat me simbol valute ("800€"/"800 euro"/"800 lek") —
    numri me valute NUK është kurrë token-modeli (Faza 1, #1b). Heqja
    s'varet nga fx: edhe pa kurs, "800" nuk duhet të kërkohet në titull.
    """
    q = raw or ""
    # RENDI KA RËNDËSI: mijëshet PARA heqjes "N lek" — përndryshe
    # "10.000 lek" humbet "000 lek" dhe "10" mbetet token-modeli.
    q = re.sub(r"\d{1,3}(?:[.,]\d{3})+\s*(?:€|euros?\b|eur\b)", " ", q,
               flags=re.IGNORECASE)
    q = re.sub(r"\d{1,3}(?:[.,]\d{3})+", " ", q)
    q = re.sub(r"\b\d+\s*(?:€|euros?\b|eur\b)", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\b\d{1,3}\s*(?:lek[e]?)\b", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\b\d+\s*(mij[eë]|mije|k)\b", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\b\d{4,6}\b", " ", q)
    return norm(q)


# ---------------------------------------------------------------- token match / search
def token_match(qtok, text_tokens):
    """Exact token; prefix match (query token length >= 3); fuzzy match for
    alphabetic tokens length >= 4 via difflib.SequenceMatcher ratio >= 0.92
    (Python approximation of the website's Levenshtein <=1/<=2).

    Two passes, same result as the reference's interleaved loop: exact and
    prefix matches are checked for every token first, and difflib runs only
    when nothing matched — and only on tokens whose length can possibly
    reach ratio 0.92 (a length gap d caps the ratio at 2*min/(2*min+d), so
    d > 0.174*min can never pass). This keeps a bot search over ~19k
    groups fast instead of running SequenceMatcher on every token pair.
    """
    if qtok.isdigit():
        return qtok in text_tokens
    for t in text_tokens:
        if t == qtok:
            return True
        if len(qtok) >= 3 and t.startswith(qtok):
            return True
    if qtok.isalpha() and len(qtok) >= 4:
        max_gap = max(0, int(0.174 * len(qtok)))
        for t in text_tokens:
            if len(t) >= 4 and abs(len(t) - len(qtok)) <= max_gap:
                if SequenceMatcher(None, qtok, t).ratio() >= 0.92:
                    return True
    return False


def search_groups(groups, query, aliases=None, fx=None):
    """Port of the reference search_groups.

    groups = MERGED groups; returns (hits, budget, core).
    hits are in catalogue order (unranked); use search_ranked() / answer_ai()
    for the relevance ranking. `fx` = kursi EUR→LEK (report.json); pa të,
    shumat € njihen si buxhet vetëm si None (shih parse_budget).
    """
    aliases = aliases or {}
    budget = parse_budget(query, fx=fx)
    q = strip_budget_tokens(query, budget)
    # feature constraints (NOT stopwords): wireless / wired
    wireless = bool(re.search(r"\b(pa kabllo|wireless|bluetooth|tws)\b", q))
    wired_only = bool(re.search(r"\b(me kabllo|wired)\b", q))
    q = re.sub(r"\b(pa kabllo|wireless|bluetooth|tws|me kabllo|wired)\b", " ", q)
    if "fshese me korrent" in q or "fshesa me korrent" in q:
        q = q.replace("fshese me korrent", "fshese").replace(
            "fshesa me korrent", "fshese")
    or_groups, numbers, core = [], [], []
    for t in q.split():
        # split ps5 / ps4 / xbox360 into base + required number
        m = re.match(r"^(ps|psp|xbox)(\d+)$", t)
        if m:
            numbers.append(m.group(2))
            or_groups.append(SYN.get(
                t, {t, "playstation" if m.group(1) == "ps" else t}))
            core.append(t)
            continue
        if t.isdigit():
            numbers.append(t)
            continue
        if t in STOPWORDS or t in IGNORE:
            continue
        or_groups.append(SYN.get(t, {t}))
        core.append(t)
    # Faza 1 (#1c): trajta e shquar → e pashquar, para çdo kontrolli.
    core = [CORE_CANON.get(c, c) for c in core]
    hits = []
    for g in groups:
        hay = norm(g.get("title", "") + " " + g.get("brand", "") + " "
                   + g.get("cat", "") + " " + g.get("pcat", "") + " "
                   + " ".join(o.get("title", "") for o in g.get("offers", [])))
        toks_hay = set(hay.split())
        for a in aliases.get(g["key"], []):
            toks_hay |= set(norm(a).split())
        ok = True
        for grp in or_groups:
            if not any(token_match(qt, toks_hay) for qt in grp):
                ok = False
                break
        if not ok:
            continue
        for n in numbers:
            if n not in toks_hay:
                ok = False
                break
        if not ok:
            continue
        # wireless constraint: title must say wireless/bluetooth/tws and not wired
        # D10 (2026-11-19): 'earbuds'/'airpods' in title count as wireless too
        # (mirrors the site round-2 rule; 'me kabllo'/'wired' still excludes)
        ttitle = norm(g.get("title", ""))
        if wireless:
            if not re.search(r"(wireless|bluetooth|\btws\b|pa kabllo|earbuds|airpods)", ttitle):
                continue
            if re.search(r"(me kabllo|wired|3\.5 mm|3,5 mm)", ttitle):
                continue
        if wired_only and not re.search(r"(me kabllo|wired)", ttitle):
            continue
        # landlines are not mobile phones
        if "fiks" in ttitle and any(
                c in ("telefon", "celular", "iphone", "ajfon") for c in core):
            continue
        hits.append(g)
    return hits, budget, core


def _relevance(g, core):
    """Port of the reference relevance(): category match, then title-start."""
    score = 0
    cat_toks = set(norm((g.get("cat") or "") + " "
                        + (g.get("pcat") or "")).split())
    for c in core:
        if c in CATMAP and cat_toks & CATMAP[c]:
            score += 4
            break
    title_toks = norm(g.get("title", "")).split()
    dev = [t for t in core if t not in STOPWORDS]
    if dev:
        first = SYN.get(dev[0], {dev[0]})
        if title_toks and title_toks[0] in first:
            score += 2
        if len(dev) > 1 and len(title_toks) > 1:
            second = SYN.get(dev[1], {dev[1]}) if not dev[1].isdigit() else {dev[1]}
            if title_toks[1] in second:
                score += 1
    # console queries: hardware beats games (same cat/pcat for both)
    if any(c in ("ps5", "ps4", "xbox", "switch", "nintendo", "playstation")
           for c in core):
        hw = {"slim", "pro", "console", "disc", "digital", "1tb", "2tb",
              "512gb", "825gb", "oled", "lite"}
        if hw & set(title_toks):
            score += 4
        if title_toks and title_toks[0] in ("ps5", "ps4", "xbox"):
            score -= 2
    return score


def _device_filter(g, core):
    """Shared device/accessory filter of the reference answer loop.

    Returns True when the group may be shown as the device itself:
    the device word (or a synonym) is among the title's FIRST 3 tokens,
    and the group is not an accessory.
    """
    if not core:
        return True
    ttoks = norm(g.get("title", "")).split()
    first_set = SYN.get(core[0], {core[0]})
    if not any(t in ttoks[:3] for t in first_set):
        return False
    if is_accessory(g, core):
        return False
    return True


def _smartphone_ok(g, core, query):
    """'telefon i mirë' means a smartphone, not a desk/basic phone."""
    if "mire" not in norm(query) and "mirë" not in (query or ""):
        return True
    tt = norm(g.get("title", ""))
    if ("tavoline" in tt or "dect" in tt or "pa tel" in tt
            or "sekretari" in tt or "receptor" in tt):
        return False
    if any(c in ("telefon", "celular", "iphone", "ajfon") for c in core):
        if not (SMARTPHONE_WORDS & set(tt.split())):
            return False
    return True


def search_ranked(groups, query, aliases=None, limit=5):
    """Keyword search: search_groups + device/accessory filter (when the
    query names a device class) + relevance ranking, then cheapest price.

    This is what the bot's free-text keyword path uses, so that e.g. "ps5"
    returns the console (not a controller) and "fshese me korrent" returns
    the cheapest real vacuum — the same tops as the website's AI answers.
    """
    hits, _budget, core = search_groups(groups, query, aliases)
    device_query = any(c in DEVICE_CORES for c in core)
    ranked = []
    for g in hits:
        if device_query and not _device_filter(g, core):
            continue
        if not _smartphone_ok(g, core, query):
            continue
        c = cheapest_valid(g)
        if not c:
            continue
        ranked.append((-_relevance(g, core), c["price"], g, c))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [(g, c) for _neg, _price, g, c in ranked[:limit]]


# ---------------------------------------------------------------- offers: used / vitrine / suspicious / cheapest
def is_used(offer, gtitle=""):
    """Check the RAW titles: norm() strips the hyphen in the "U-" prefix."""
    for raw in ((offer or {}).get("title", ""), gtitle):
        u = (raw or "").strip().upper()
        if (u.startswith("U-") or " USED" in u or "I PËRDORUR" in u
                or "I PERDORUR" in u or "REFURBISHED" in u
                or "I RINOVUAR" in u or "SECOND HAND" in u
                or "OPEN BOX" in u or "ISH EKSPOZITE" in u):
            return True
    return False


def is_vitrine(offer, gtitle=""):
    for raw in ((offer or {}).get("title", ""), gtitle):
        if "VITRIN" in (raw or "").upper():
            return True
    return False


def group_median(group):
    prices = [o["price"] for o in (group or {}).get("offers", [])
              if o.get("price")]
    if not prices:
        return 0
    return statistics.median(prices)


def is_suspicious(offer, median):
    price = (offer or {}).get("price")
    if not price or not median:
        return False
    return price > 3 * median or price < median / 3


def comparable_offers(group):
    """Offers excluding suspicious, vitrinë and used."""
    med = group_median(group)
    out = []
    gtitle = (group or {}).get("title", "")
    for o in (group or {}).get("offers", []):
        if not o.get("price"):
            continue
        if is_used(o, gtitle) or is_vitrine(o, gtitle):
            continue
        if is_suspicious(o, med):
            continue
        out.append(o)
    return out


def cheapest_valid(group):
    """Lowest-price comparable offer with in_stock is not False
    (True or None allowed)."""
    valid = [o for o in comparable_offers(group)
             if o.get("in_stock") is not False]
    if not valid:
        return None
    return min(valid, key=lambda o: o["price"])


def cheapest_in_stock(group):
    """Lowest-price comparable offer with in_stock is True (None if none)."""
    valid = [o for o in comparable_offers(group)
             if o.get("in_stock") is True]
    if not valid:
        return None
    return min(valid, key=lambda o: o["price"])


def canonical_cheapest(group):
    """PURE computation only (the fallback): {"cheapest": offer|None,
    "in_stock": offer|None}. Never reads scanner fields — use
    scanner_cheapest() for the scanner-field preference path."""
    return {"cheapest": cheapest_valid(group),
            "in_stock": cheapest_in_stock(group)}


def scanner_cheapest(group):
    """Scanner-field preference (D4 fix, pilot pattern).

    Returns (best, best_in_stock) from the scanner fields, or (None, None)
    when the fields must not be trusted — the caller then falls back to
    the pure computation (cheapest_valid / cheapest_in_stock).

    Trust rule (spec §2.1, Part A): group["best"] is used ONLY when the
    group also carries "best_in_stock" — that key is the marker Part A
    redefined "best" to cheapest_valid. An old "best" without
    "best_in_stock" is NEVER trusted. A field that is present but None or
    malformed (no price) counts as absent for that slot: "best" falls back
    to "best_in_stock" (pilot pattern: best or best_in_stock), and a None
    slot is left None for the caller to fill purely.
    Merged groups are NOT handled here — the caller must exclude them
    (their scanner fields describe only the first raw group, not the
    merged product; see bot_worker._canonical_cheapest).
    """
    if not group or "best_in_stock" not in group:
        return None, None

    def _valid(o):
        return o if isinstance(o, dict) and o.get("price") else None

    best = _valid(group.get("best")) or _valid(group.get("best_in_stock"))
    bis = _valid(group.get("best_in_stock"))
    return best, bis


def scanner_verdict(group):
    """group["verdict"] from the scanner, or None when absent / invalid.
    Valid verdicts are exactly "BLI TANI" | "PRIT" | "SHMANG" | "FLAT"
    (spec §2.3 + site rule A3: the scanner writes "FLAT" for a flat
    history; the bot displays it as "Çmim i qëndrueshëm", never BLI TANI).
    Merged groups are NOT handled here — the caller must exclude them."""
    v = (group or {}).get("verdict")
    return v if v in ("BLI TANI", "PRIT", "SHMANG", "FLAT") else None


# ---------------------------------------------------------------- verdict / discount
def verdict_for(history, current=None):
    """Verdict from history only. history = [[date, price], ...].

    Needs >= 2 entries, else None. chg7 = change vs the price 7 days ago
    (or the first entry if fewer than 8 points). FLAT history
    (max == min, the price never moved) -> "FLAT" (site rule A3: "at the
    historic minimum" means nothing when there was never another price,
    so a flat group is NEVER BLI TANI). chg7 >= +8% -> SHMANG;
    current <= min(history) x 1.02 -> BLI TANI; else PRIT.
    `current` defaults to the last history price; pass the group's
    cheapest VALID price when it is known (spec §2.3).
    """
    if not history or len(history) < 2:
        return None
    prices = [p for _d, p in history if p]
    if not prices:
        return None
    if max(prices) <= min(prices):
        return "FLAT"
    if current is None:
        current = history[-1][1]
    if not current:
        return None
    week_ago = history[-8][1] if len(history) > 7 else history[0][1]
    chg7 = ((current - week_ago) / week_ago) if week_ago else 0
    if chg7 >= 0.08:
        return "SHMANG"
    if current <= min(prices) * 1.02:
        return "BLI TANI"
    return "PRIT"


def discount_pct(price, old):
    if not price or not old or not old > price:
        return None
    return round((old - price) / old * 100, 1)


# ---------------------------------------------------------------- formatting / links
def fmt_price(p):
    """129990 -> "129.990" (Albanian dot grouping, never the comma form)."""
    try:
        return f"{float(p):,.0f}".replace(",", ".")
    except Exception:
        return "—"


def fmt_lek(p):
    return fmt_price(p) + " L"


_MONTHS_AL = ["janar", "shkurt", "mars", "prill", "maj", "qershor",
              "korrik", "gusht", "shtator", "tetor", "nëntor", "dhjetor"]


def fmt_date_al(iso_date):
    """'2026-11-19' -> '19 nëntor 2026'."""
    try:
        y, m, d = str(iso_date)[:10].split("-")
        return "%d %s %s" % (int(d), _MONTHS_AL[int(m) - 1], y)
    except Exception:
        return str(iso_date)


def slugify(s):
    """Port of the website's slugify literally: norm -> [^a-z0-9]+ -> '-',
    strip leading/trailing '-', slice to 60, fallback 'produkt'."""
    slug = re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")[:60]
    return slug or "produkt"


def product_path(group):
    return "/p/" + slugify(group.get("title", "")) + "-" + str(group.get("key", ""))


def product_url(group):
    return SITE_URL + product_path(group)


# ---------------------------------------------------------------- merge
def _merge_key_norm(s):
    """Website-faithful merge-key normalisation (see module docstring):
    lowercase, ë→e, ç→c, NFD accent strip — punctuation and spacing kept,
    so titles that differ only by punctuation do NOT merge."""
    s = (s or "").lower().replace("ë", "e").replace("ç", "c")
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def merge_groups(groups):
    """Website buildCatalog merge: key = norm(title)|norm(brand)|norm(cat||pcat);
    offers concatenated, first group's key/title kept.

    Returns (merged_groups, n_merged_away). Expected on 2026-11-19 data:
    19.497 -> 19.370 groups (n_merged_away = 127).
    """
    buckets = {}
    order = []
    n_merged = 0
    for g in groups:
        k = (_merge_key_norm(g.get("title", "")).strip() + "|"
             + _merge_key_norm(g.get("brand", "")) + "|"
             + _merge_key_norm(g.get("cat") or g.get("pcat") or ""))
        if k in buckets:
            m = buckets[k]
            m["offers"] = m.get("offers", []) + list(g.get("offers", []))
            # The merged group concatenates offers from several raw groups,
            # so per-raw-group scanner fields (cheapest / verdict) no longer
            # describe it: the bot recomputes them purely for merged groups.
            m["_merged"] = True
            m.setdefault("_merged_keys", []).append(g.get("key"))
            stores = []
            for o in m["offers"]:
                if o.get("store") not in stores:
                    stores.append(o.get("store"))
            m["stores"] = stores
            m["n_stores"] = len(stores)
            n_merged += 1
        else:
            c = dict(g)
            c["offers"] = list(g.get("offers", []))
            c["_merged"] = False
            c["_merged_keys"] = [g.get("key")]
            buckets[k] = c
            order.append(k)
    return [buckets[k] for k in order], n_merged


# ---------------------------------------------------------------- accessory / language / AI
def is_accessory(group, core):
    tt = norm(group.get("title", ""))
    toks = set(tt.split())
    # pcat 'aksesore' = accessory (reliable when present)
    if norm(group.get("pcat", "")) == "aksesore":
        return True
    # title markers, prefix-matched
    if any(t.startswith(m) for t in toks for m in ACCESSORY_MARKERS):
        return True
    # paper notebook, not a laptop: 'notebook' with no laptop specs
    if "notebook" in toks and not (toks & {"ram", "intel", "amd", "core",
                                           "celeron", "ryzen", "ssd", "hdd",
                                           "gb", "ghz"}):
        return True
    return False


# Faza 1 (2026-11-20, audit §13.3/#14-15): markerët EN maten me Kufi
# FJALE (\b), jo si nënvarg — "iphone" përmban "phone" dhe "laptop" është
# fjalë e huazuar shqipe; të dyja kthenin gabimisht "en" për pyetje të
# vetme shqipe. "laptop"/"phone" u hoqën nga markerët: një fjalë produkti
# s'dëshmon gjuhën. "headphones" e vetme mbetet EN (fjalë angleze).
_EN_WORDS = ("cheapest", "under", "below", "best", "good", "wireless",
             "headphones", "earphones", "earbuds", "airpods", "price",
             "where", "what", "how", "much")
_EN_RE = re.compile(r"\b(" + "|".join(_EN_WORDS) + r")\b")
_SQ_MARKERS = ("më", "me", "deri", "nën", "nen", "ku", "është", "është",
               "student", "lirë", "liri", "kërko", "fshes", "kufje",
               "televizor")


def detect_lang(query):
    """'sq' | 'en' (spec §2.5): EN if the query has no ë/ç AND contains an
    English marker word AND no Albanian marker; else SQ."""
    q = query or ""
    low = q.lower()
    if ("ë" not in q and "ç" not in q
            and _EN_RE.search(low)
            and not any(m in low for m in _SQ_MARKERS)):
        return "en"
    return "sq"


def _pick_reason(g, offer, lang):
    nstores = g.get("n_stores", len(g.get("offers", [])))
    drop = g.get("discount_pct")
    if lang == "en":
        reason = ("Cheapest %s at %s · %s stores"
                  % (fmt_lek(offer["price"]), offer.get("store_name"),
                     nstores))
        if drop:
            reason += " · −%s%%" % drop
    else:
        reason = ("Çmimi më i lirë %s te %s · %s dyqane"
                  % (fmt_lek(offer["price"]), offer.get("store_name"),
                     nstores))
        if drop:
            reason += " · ulje %s%%" % drop
    return reason


def answer_ai(groups, query, aliases=None, fx=None, fx_date=None):
    """Port of the reference answer logic (spec §2.5).

    Returns {"lang", "picks": [{group, offer, reason}], "no_match": bool,
    "fallback": {group, offer} | None, "budget": lek|None,
    "budget_eur": EUR|None, "fx": rate|None, "fx_date": date|None}.
    Budget is a HARD limit: picks never exceed it. When nothing is within
    budget, `fallback` is the cheapest REAL device of that category (same
    filters, no budget cap); when there is nothing at all, `fallback` is
    None and `no_match` is True. Accessories are never shown as devices.
    `fx` = kursi EUR→LEK (report.json: fx.EUR_ALL); `fx_date` = data e tij.
    """
    lang = detect_lang(query)
    hits, budget, core = search_groups(groups, query, aliases, fx=fx)
    budget_eur = eur_amount(query)
    fx_used = fx if (budget_eur is not None and fx and fx > 0) else None
    device_query = any(c in DEVICE_CORES for c in core)
    ranked = []
    for g in hits:
        if device_query and not _device_filter(g, core):
            continue
        if not _smartphone_ok(g, core, query):
            continue
        c = cheapest_valid(g)
        if not c:
            continue
        if budget and c["price"] > budget:
            continue
        # laptop must be real (spec §2.5: picks AND fallback)
        if "laptop" in core:
            if not (LAPTOP_SPEC & set(norm(g.get("title", "")).split())):
                continue
        ranked.append((-_relevance(g, core), c["price"], g, c))
    ranked.sort(key=lambda x: (x[0], x[1]))
    picks = [{"group": g, "offer": c,
              "reason": _pick_reason(g, c, lang)}
             for _neg, _price, g, c in ranked[:3]]
    fallback = None
    if not ranked and hits:
        # honest fallback: cheapest actual DEVICE (same filters, no budget cap)
        allr = []
        for g in hits:
            if device_query and not _device_filter(g, core):
                continue
            if not _smartphone_ok(g, core, query):
                continue
            c = cheapest_valid(g)
            if c:
                allr.append((c["price"], g, c))
        allr.sort(key=lambda x: x[0])
        # for laptop queries the fallback must be a real laptop, not a part
        if "laptop" in core and allr:
            spec = [r for r in allr
                    if LAPTOP_SPEC & set(norm(r[1].get("title", "")).split())]
            if spec:
                allr = spec
                allr.sort(key=lambda x: x[0])
        if allr:
            price, g, c = allr[0]
            fallback = {"group": g, "offer": c}
    return {"lang": lang, "picks": picks,
            "no_match": not picks, "fallback": fallback,
            "budget": budget, "budget_eur": budget_eur,
            "fx": fx_used, "fx_date": fx_date if fx_used else None}


# ---------------------------------------------------------------- week drops
def week_drops(groups, history):
    """Shared week-drops computation (spec §2.7).

    history = history.json dict {groups: {key: {name, history: [[date, price]]}}}.
    history >= 3 days, old = price 7 days ago (or oldest point), drop >= 3%,
    price >= 3.000 L, sorted by pct desc.
    Returns [{key, title, price, old, pct, store, store_name, url}].
    """
    bykey = {}
    for g in (groups or []):
        # A history entry may be keyed by a raw group that was merged away;
        # it still belongs to the merged group (same product).
        for k in g.get("_merged_keys", [g.get("key")]):
            bykey[k] = g
    hist_groups = {}
    if isinstance(history, dict):
        hist_groups = history.get("groups", {}) or {}
    out = []
    for key, entry in hist_groups.items():
        hist = (entry or {}).get("history", []) or []
        if len(hist) < 3:
            continue
        g = bykey.get(key)
        c = cheapest_valid(g) if g else None
        if not c:
            continue
        price = c["price"]
        if not price or price < 3000:
            continue
        old = hist[-8][1] if len(hist) > 7 else hist[0][1]
        if not old or old <= price:
            continue
        pct = round((old - price) / old * 100, 1)
        if pct < 3:
            continue
        out.append({"key": key,
                    "title": (g.get("title") if g else None)
                             or entry.get("name") or key,
                    "price": price, "old": old, "pct": pct,
                    "store": c.get("store"), "store_name": c.get("store_name"),
                    "url": c.get("url")})
    out.sort(key=lambda d: -d["pct"])
    return out
