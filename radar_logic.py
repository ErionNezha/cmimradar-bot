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
- verdict_for(history, current=None, family_best=None): the spec §6
  signature is verdict_for(history); the optional `current` lets the
  caller pass the group's cheapest VALID price (spec §2.3: the verdict
  rests on the cheapest valid price, not on the last history point).
  With current=None it uses the last history price, exactly as the
  website does when the history's last point IS the current price.
  The optional `family_best` is group["family_best_in_stock"]
  (build.py): BLI TANI -> PRIT when current >= 1.10 * family_best
  from a different group (site rule, 2026-11-23 item 2). Rule
  (2026-11-23, item 6, spec item 3): w30 = last 30 history points
  (max 30); FLAT when (max(w30)-min(w30))/min(w30) < 0.03; BLI TANI
  only when current <= min(w30) AND current <= 0.95 * median(w30);
  chg7 >= +8% -> SHMANG; else PRIT.
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
    # FINAL FIX PASS §6 (2026-11-22): "stok" është filtër, jo term kërkimi
    # (si te faqja: AI_STOP) — përndryshe "iphone 17 më i liri në stok"
    # kërkon fjalën "stok" në tituj dhe kthen ZERO rezultate.
    "stok", "stokun", "stokut", "stock",
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
                "aspirator",
                # FINAL FIX PASS §6 (2026-11-22): si AI_DEVCORES te faqja —
                # pa 'frigorifer' filtri i pajisjes/aksesorëve nuk zbatohej
                # dhe "frigorifer deri 1000 L" përgjigjej me aksesorë
                # (bllokues sigurie, filtër BRITA) në vend të frigoriferit.
                "frigorifer", "frigoriferi", "fridge", "refrigerator"}

LAPTOP_SPEC = {"ram", "intel", "amd", "core", "celeron", "ryzen", "ssd",
               "hdd", "emmc", "ghz", "kryo", "snapdragon", "mediatek",
               "n4020", "n100", "i3", "i5", "i7", "i9"}

SMARTPHONE_WORDS = {"galaxy", "redmi", "iphone", "pixel", "poco", "honor",
                    "motorola", "realme", "oneplus", "nothing", "infinix",
                    "tecno", "vivo", "oppo", "nokia", "xiaomi", "samsung",
                    "huawei", "itel", "tcl", "zte", "alcatel"}


# Variant-signature rule (2026-11-22, audit item 4): "iphone 17" ≠ "17 Air"
# ≠ "17 Pro" ≠ "17 Pro Max" ≠ "17e". Port of the site build.py MODIFIERS
# (build.py:605-607) + variant_sig() (build.py:656), adapted for QUERY
# matching: connectivity tokens (4g/5g/3g/cellular) and audio tokens
# (anc/nc) are NOT model variants a shopper distinguishes — including them
# would drop e.g. "Galaxy S25 5G" for the query "galaxy s25" — so the
# query-matching set keeps only the model-variant words. "cellular" is
# excluded for the same reason AND because it is a store prefix
# ("CELULAR iPhone 17 ...") that must never enter a variant signature.
VARIANT_TOKENS = {"ultra", "pro", "proplus", "max", "plus", "lite", "fe",
                  "mini", "note", "fold", "flip", "xl", "xs", "air", "edge",
                  "slim", "neo", "se"}
_VARIANT_SUFFIX = ("air", "pro", "proplus", "max", "plus", "ultra", "mini",
                   "lite", "fe", "se", "edge", "slim", "fold", "flip", "note",
                   "xl", "xs", "neo")
_STORAGE_RE = re.compile(r"^\d+(gb|tb|mb)$")
_CONNECTIVITY = {"4g", "5g", "3g", "lte", "cellular"}


def _variant_sig(tokens):
    """Canonical variant signature of a token list (query core or group
    title). {} for a base model ("iphone 17"), {"air"} for "17 Air",
    {"pro", "max"} for "17 Pro Max", {"17e"} for "17e". Connectivity
    (5g/cellular), storage (256gb) and colours never enter the signature.
    Fused tokens are split: "s25plus" -> "plus", "17pro" -> "pro"; a fused
    model suffix ("17e", "s10e", "a52s") is kept whole, so "17e" still
    differs from "17" — while spec tokens glued to a number ("4k",
    "65w", "120hz", "48mp") never enter the signature (see below).
    """
    sig = set()
    for t in tokens or []:
        if t in VARIANT_TOKENS:
            sig.add(t)
            continue
        if t in _CONNECTIVITY or _STORAGE_RE.match(t):
            continue
        m = re.match(r"^([a-z]*)(\d+)([a-z]+)$", t)
        if m:
            suf = m.group(3)
            if suf in _VARIANT_SUFFIX:
                sig.add(suf)
            elif (len(suf) == 1 and suf in ("e", "s", "c")
                    and len(m.group(2)) >= 2):
                # Model suffix glued to its number: "17e" ≠ "17", "a52s" ≠
                # "a52", "s10e" ≠ "s10". Anything else glued to a number
                # is a spec, not a variant — "4k" (resolution), "65w"
                # (power), "120hz" (refresh), "48mp" (camera), "10000mah"
                # (battery), "120mm" (size) must NEVER enter the signature
                # (verified over live 2026-11-22 titles: thousands of such
                # tokens; a single-letter suffix other than e/s/c, or a
                # 1-digit number ("6e" = Wi-Fi 6E), is a spec too).
                sig.add(t)
    return frozenset(sig)


def _has_model_token(tokens):
    """True when the query names a specific model: a required number
    ("17", "55") or an alphanumeric model token ("s25", "a55", "17e").
    Storage ("256gb") and connectivity ("5g") never count. Without a
    model token ("macbook", "iphone", "laptop") the variant rule does not
    apply — a bare brand/line query must not lose e.g. MacBook Air."""
    for t in tokens or []:
        if _STORAGE_RE.match(t) or t in _CONNECTIVITY:
            continue
        if t.isdigit() or (re.search(r"\d", t)
                           and re.match(r"^[a-z0-9]+$", t)):
            return True
    return False


def _accessory_led(core):
    """True when the query's first core token is an accessory word
    ("kase për iphone 17 air"): the query is about the accessory, not
    the device — the variant rule must not touch it."""
    if not core:
        return False
    return any(core[0].startswith(m) for m in ACCESSORY_MARKERS)


def _variant_context(query, budget, core):
    """(apply, query_sig) for the variant-signature rule.

    Applies only when the query names a device class (deviceQuery) AND a
    specific model (a number / model token) AND is not accessory-led.
    The caller compares the candidate GROUP TITLE's signature to
    query_sig — never the haystack (offer titles carry accessories of
    other variants).
    """
    if not any(c in DEVICE_CORES for c in core) or _accessory_led(core):
        return False, frozenset()
    q_toks = strip_budget_tokens(query, budget).split()
    if not _has_model_token(q_toks):
        return False, frozenset()
    return True, _variant_sig(q_toks)


def _variant_ok(title, query_sig, apply):
    """Variant-signature equality on the GROUP TITLE tokens."""
    if not apply:
        return True
    return _variant_sig(norm(title).split()) == query_sig


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
        # Store/brand-prefixed titles ("CELULAR iPhone 17 ...", "Apple
        # iPhone 17 ..."): the device word
        # is not token[0] but token[1]/[2] — the +2 bonus (and the +1 for
        # the following token) are measured from where the device word
        # actually sits, within the first 3 tokens (same window as
        # _device_filter). Without a prefix the offset is 0: unchanged.
        idx = next((i for i, t in enumerate(title_toks[:3]) if t in first),
                   None)
        if idx is not None:
            if len(dev) > 1:
                second = (SYN.get(dev[1], {dev[1]})
                          if not dev[1].isdigit() else {dev[1]})
            else:
                second = set()
            # The offset bonus is real only when the token after the
            # device word confirms the same model: dev[1] ("Galaxy" after
            # "Samsung") or a number — the model number itself, which
            # search_groups keeps out of `core` ("17" after "iPhone" in
            # "CELULAR iPhone 17 ..."). Otherwise a buried device word
            # ("Sony PS5 Persona", "Apple iPhone Leather Wallet", "Koke
            # karikimi iPhone") would outrank the device itself.
            confirmed = (idx == 0 or (len(title_toks) > idx + 1
                                      and (title_toks[idx + 1] in second
                                           or title_toks[idx + 1].isdigit())))
            if confirmed:
                score += 2
                if len(dev) > 1 and title_toks[idx + 1] in second:
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
    apply_variant, q_sig = _variant_context(query, _budget, core)
    want_in_stock = _wants_in_stock(query)
    ranked = []
    for g in hits:
        if device_query and not _device_filter(g, core):
            continue
        if not _variant_ok(g.get("title", ""), q_sig, apply_variant):
            continue
        if not _smartphone_ok(g, core, query):
            continue
        c = _lead_offer(g, want_in_stock)
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


def is_no_box(offer, gtitle=""):
    """Mirror site isNoBox (index.html): data flag OR title patterns."""
    if (offer or {}).get("is_no_box"):
        return True
    for raw in ((offer or {}).get("title", ""), gtitle):
        u = (raw or "").upper()
        if ("NO BOX" in u or "NO-BOX" in u or "NOBOX" in u
                or "PA KUTI" in u):
            return True
    return False


def is_usa(offer, gtitle=""):
    """US-carrier version (2026-11-23, Item 1): mirror site isUsaOffer
    (index.html) and the data flag is_usa (build.py) — data flag OR
    raw-title carrier markers (AT&T / T-Mobile / Verizon / Straight
    Talk / USA / SHBA). Same rule as eSIM/unverified: a USA offer is
    never the lead when a plain EU offer exists. The data-side fallback
    (scanner_cheapest trusting rebuilt data `best`) still answers
    USA/eSIM-only groups; the pure path simply yields no USA lead."""
    if (offer or {}).get("is_usa"):
        return True
    for raw in ((offer or {}).get("title", ""), gtitle):
        if re.search(r"AT&T|T-Mobile|Verizon|Straight[ -]?Talk|\bUSA\b|"
                     r"US version|Unlocked USA|\bSHBA\b",
                     raw or "", re.IGNORECASE):
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
        # FINAL FIX PASS §6 (2026-11-22): "çmim nga" (price_from) nuk është
        # kurrë "më i liri" — si te faqja (FINAL POLISH: 221 oferta
        # price_from të përjashtuara nga best/drops/verdicts). Pa këtë,
        # "iphone 17 më i liri në stok" kryesohej nga një ofertë "nga"
        # (73.500 L) në vend të çmimit real në stok (74.900 L, Tigo).
        if o.get("price_from"):
            continue
        # FINAL FIX PASS §6 (2026-11-22): ofertat e flamuruara
        # is_unverified_variant (p.sh. të gjitha iPhone-t e Celular-it)
        # nuk janë kurrë "më i liri" — si te faqja (build.py: oferta e
        # flamuruar përjashtohet nga best/verdicts). Pa këtë, "iphone 17
        # më i liri" kryesohej nga Celular 71.146 L (in_stock=None) në
        # vend të çmimit real (74.900 L, Tigo). Zbuluar në rishikim para
        # deploy-it të bot-it; commit-i 1e898a8 e premtonte por s'e
        # zbatonte dot (asnjë referencë is_unverified_variant në kod).
        if o.get("is_unverified_variant"):
            continue
        # Site rule (index.html comparableOffers, linja 711): "Pa kuti"
        # nuk është kurrë "më i liri" — pa këtë, "iphone 17 pro max"
        # kryesohej nga German Computers 97.900 L (NEW NO BOX E-SIM)
        # në vend të 112.900 L (3V Fejzo, Fizik).
        if is_no_box(o, gtitle):
            continue
        # US-carrier versions (2026-11-23, Item 1): AT&T / T-Mobile /
        # Verizon / Straight Talk / USA / SHBA — never the lead when a
        # plain EU offer exists (same rule as eSIM/unverified). This
        # single point fixes cheapest_valid / cheapest_in_stock /
        # _lead_offer / canonical_cheapest (all consume this function).
        if is_usa(o, gtitle):
            continue
        out.append(o)
    return out


def cheapest_valid(group):
    """Lowest-price comparable offer with in_stock is not False
    (True or None allowed). Mirror site cheapest(): prefer a NON-eSIM
    offer when one exists (an eSIM offer leads only if it is the only
    comparable one). USA offers are excluded outright by
    comparable_offers() (Item 1, 2026-11-23) — e.g. Pro Max leads with
    shpresa 114.990, never the AT&T 112.900 or the USA e-SIM 112.000."""
    valid = [o for o in comparable_offers(group)
             if o.get("in_stock") is not False]
    plain = [o for o in valid if not o.get("is_esim")]
    if plain:
        valid = plain
    if not valid:
        return None
    return min(valid, key=lambda o: o["price"])


def cheapest_in_stock(group):
    """Lowest-price comparable offer with in_stock is True (None if none).
    Mirror site cheapestInStock(): prefer NON-eSIM when one exists.
    USA offers are excluded outright by comparable_offers()."""
    valid = [o for o in comparable_offers(group)
             if o.get("in_stock") is True]
    plain = [o for o in valid if not o.get("is_esim")]
    if plain:
        valid = plain
    if not valid:
        return None
    return min(valid, key=lambda o: o["price"])


def _wants_in_stock(query):
    """FINAL FIX PASS §6 (2026-11-22): True when the question asks for
    confirmed stock — "në stok" / "me stok" / "stokun" / "in stock".
    norm() folds ë->e, so "në" is "ne" and "stok" matches "stokun"."""
    return bool(re.search(r"\b(stok\w*|in stock)\b", norm(query or "")))


def _lead_offer(group, want_in_stock):
    """The offer that LEADS an answer for a group.

    want_in_stock (question asks "në stok") -> cheapest offer with
    in_stock is True; a group with no confirmed-stock offer has NO lead
    (caller skips it — it must never answer as "më i liri në stok" with
    an unconfirmed Celular offer). Otherwise -> cheapest_valid, the
    documented behaviour (cheapest overall + "stoku i pakonfirmuar" label
    when its stock is unconfirmed)."""
    if want_in_stock:
        return cheapest_in_stock(group)
    return cheapest_valid(group)


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
def verdict_for(history, current=None, family_best=None):
    """Verdict from history only. history = [[date, price], ...].

    Rule (2026-11-23, item 6 — identical to the site rule shipped in
    parallel in buy_signal.py: verdict_for() + index.html: verdictFor).
    Dritarja është w30 = 30 pikat e fundit të historisë (max 30);
    histori më të shkurtra përdorin të gjitha pikat e disponueshme.
    Needs >= 2 entries, else None.
    - FLAT: the price range over w30 is < 3% ((max(w30)-min(w30)) /
      min(w30) < 0.03) -> "FLAT" ("Çmim i qëndrueshëm"), NEVER BLI TANI.
      A flat w30 means "at the historic minimum" carries no information
      (e.g. iPhone 17 256GB: w30 74.000-74.900, spread 1.2% -> FLAT).
    - chg7 = change vs the price 7 days ago (or the first entry if fewer
      than 8 points); chg7 >= +8% -> SHMANG. Checked AFTER flat (the
      site checks flat first too): with a < 3% w30 range the 7-day
      change can only reach +8% when TODAY's price jumped.
    - BLI TANI only when current <= min(w30) AND current <= 0.95 *
      median(w30); else PRIT.
    - Family cap (site rule, 2026-11-23 item 2): BLI TANI -> PRIT when
      current >= 1.10 * family_best (cheapest in-stock EU price of the
      same model+storage family, from a different group); skipped when
      family_best is None or equals current. Never SHMANG.
    `current` defaults to the last history price; pass the group's
    cheapest VALID price when it is known (spec §2.3).
    `family_best` = group["family_best_in_stock"] (build.py); None when
    the field is absent (old data) — the cap is then skipped.
    """
    if not history or len(history) < 2:
        return None
    prices = [p for _d, p in history if p]
    if not prices:
        return None
    # Dritarja w30 = 30 pikat e fundit (max 30); histori më të shkurtra
    # përdorin të gjitha pikat e disponueshme. TË GJITHA kontrollet e
    # rregullit (FLAT, min, median) maten mbi w30 — jo mbi gjithë
    # historinë (korrigjim 2026-11-23, item 3: FLAT mbi gjithë historinë
    # e linte jashtë një grupi që ka qenë i qëndrueshëm 30 ditët e fundit
    # por ka lëvizur më parë).
    last30 = prices[-30:]
    mn30 = min(last30)
    mx30 = max(last30)
    if mn30 and (mx30 - mn30) / mn30 < 0.03:
        return "FLAT"
    if current is None:
        current = history[-1][1]
    if not current:
        return None
    week_ago = history[-8][1] if len(history) > 7 else history[0][1]
    chg7 = ((current - week_ago) / week_ago) if week_ago else 0
    if chg7 >= 0.08:
        return "SHMANG"
    med30 = statistics.median(last30)
    if current <= mn30 and current <= 0.95 * med30:
        verdict = "BLI TANI"
    else:
        verdict = "PRIT"
    if (verdict == "BLI TANI" and family_best is not None
            and family_best != current and current >= 1.10 * family_best):
        return "PRIT"
    return verdict


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
    §6 (2026-11-22): when the question asks "në stok", the lead offer is
    the cheapest with CONFIRMED stock (cheapest_in_stock); a group with
    no confirmed-stock offer is skipped entirely for that question.
    `fx` = kursi EUR→LEK (report.json: fx.EUR_ALL); `fx_date` = data e tij.
    """
    lang = detect_lang(query)
    hits, budget, core = search_groups(groups, query, aliases, fx=fx)
    budget_eur = eur_amount(query)
    fx_used = fx if (budget_eur is not None and fx and fx > 0) else None
    device_query = any(c in DEVICE_CORES for c in core)
    apply_variant, q_sig = _variant_context(query, budget, core)
    want_in_stock = _wants_in_stock(query)
    ranked = []
    for g in hits:
        if device_query and not _device_filter(g, core):
            continue
        if not _variant_ok(g.get("title", ""), q_sig, apply_variant):
            continue
        if not _smartphone_ok(g, core, query):
            continue
        c = _lead_offer(g, want_in_stock)
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
            if not _variant_ok(g.get("title", ""), q_sig, apply_variant):
                continue
            if not _smartphone_ok(g, core, query):
                continue
            c = _lead_offer(g, want_in_stock)
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
