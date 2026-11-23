#!/usr/bin/env python3
"""RADAR Bot — Render Background Worker (long polling 24/7).

Përgjigjet në sekonda ("si robot"), i lidhur me website-in live
(lexon products.json/report.json/history.json nga cmimradar.netlify.app).

Veçoritë:
  🔍 kërkim çmimi me tekst të lirë (kërkim i përbashkët me faqen)
  🤖 "Pyet Radarin" — përgjigje me buxhet të fortë, vetëm produkte reale
  ⌨️ menu me butona (reply keyboard)
  🔥 /oferta — oferta e ditës
  📰 /digest — abonim në top-5 uljet e ditës
  🔔 alarme uljeje via deep-link (tg_/em_) + /alerts me butona fshirjeje
  📸 oferta nga komuniteti (ruhen në bot-state function, statusi "në pritje")
  📊 përmbledhje admin kur del raport i ri

Rregullat kanonike (çmimi më i lirë me dy nivele stoku, verdikti nga
historia, kërkimi, formatimi, linket /p/<slug>-<id>) jetojnë në
radar_logic.py — boti e importon si modul fqinj dhe e përdor kudo.
Kur build.py / buy_signal.py shkruajnë fushat e reja në products.json
(group["best"] / ["best_in_stock"] / ["verdict"]), boti i parapëlqen
"best" / "best_in_stock" — "best" VETËM kur grupi ka edhe
"best_in_stock" (shenja që Part A e ripërcaktoi best-in si çmimi më i
lirë valid; best-i i vjetër pa best_in_stock NUK besohet kurrë, D4
2026-11-19); përndryshe i llogarit pastër me radar_logic. VERDIKTI
është përjashtim (2026-11-23, item 6): kur historia është e
disponueshme, verdikti llogaritet me rregullin e botit
(radar_logic.verdict_for) — fusha group["verdict"] e skanerit është një
fotografi e shkruar nga buy_signal.py e skanimit të fundit dhe vjetrohet
kur rregulli ndryshon; ajo përdoret vetëm si fallback kur historia
mungon. Të dyja rrugët japin të njëjtën përgjigje sapo skaneri të
shkruajë rregullin e ri (të njëjtin history.json + family_best).

Alarmet / digest / përmbledhja admin DËRGOHEN VETËM nga cron-i
(check_remote_alerts.py, pas çdo skanimi). Kopjet e fjetura që ishin
këtu (run_jobs, check_price_alerts, check_push_alerts, send_push_digest,
send_daily_digest, admin_summary) u HOQËN më 2026-11-19 (bot-sync) —
boti në webhook mode nuk i ekzekutonte kurrë. Këtu mbetet vetëm
heartbeat-i, të cilin e lexon health_check C-BOT-2.

State-i mbahet në Netlify Blobs via function-i `bot-state`
(përndryshe disku efemer i Render-it do ta humbiste).

Env vars (Render dashboard):
  TG_BOT_TOKEN, BOT_KEY, TG_ADMIN_CHAT, SITE_URL
"""
import base64
import html as htmlmod
import json
import os
import random
import re
import statistics
import time
import traceback
import urllib.parse
import urllib.request

import radar_logic

def _load_dotenv():
    """Botkeep i ruan variablat në file-in .env (rrënja e projektit), jo në env të procesit.

    Ngarkohen me setdefault: env-i real (p.sh. Render) ka gjithmonë përparësi.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


_load_dotenv()

# ---------------------------------------------------------------- config
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
BOT_KEY = os.environ.get("BOT_KEY", "").strip()
ADMIN_CHAT = os.environ.get("TG_ADMIN_CHAT", "").strip()
SITE_URL = os.environ.get("SITE_URL", "https://cmimradar.netlify.app").rstrip("/")
STATE_URL = SITE_URL + "/.netlify/functions/bot-state"

DATA_TTL = 15 * 60        # rifreskim të dhënash nga website
HEARTBEAT_EVERY = 5 * 60

for _v in ("TG_BOT_TOKEN", "BOT_KEY"):
    if not globals()[_v]:
        raise SystemExit("Mungon env var: " + _v)

# Formatimi / linket vijnë nga radar_logic (rregullat kanonike, §2.6).
fmt_price = radar_logic.fmt_price

# ---------------------------------------------------------------- telegram
def tg(method, payload, timeout=40):
    url = "https://api.telegram.org/bot%s/%s" % (TG_BOT_TOKEN, method)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        print("  !! tg.%s: %s" % (method, str(e)[:120]), flush=True)
        return None


def tg_send(chat_id, text, markup=None, preview=False):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": not preview}
    if markup is not None:
        payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    r = tg("sendMessage", payload)
    return bool(r and r.get("ok"))


def tg_answer_callback(cb_id, text=""):
    r = tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:180]})
    return bool(r and r.get("ok"))


def tg_get_file(file_id):
    r = tg("getFile", {"file_id": file_id}) or {}
    return (r.get("result") or {}).get("file_path", "")


def tg_download_file(file_path):
    url = "https://api.telegram.org/file/bot%s/%s" % (TG_BOT_TOKEN, file_path)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except Exception as e:
        print("  !! download: %s" % str(e)[:120], flush=True)
        return None

# ---------------------------------------------------------------- bot-state (Netlify Blobs via function)
def bs_request(method, body=None):
    headers = {"x-bot-key": BOT_KEY}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(STATE_URL, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        try:
            return json.loads(body)
        except Exception:
            return body.decode("utf-8", "replace")  # p.sh. "state-ok" (tekst)
    except Exception as e:
        print("  !! bot-state %s: %s" % (method, str(e)[:120]), flush=True)
        return None


def bs_load():
    d = bs_request("GET")
    if isinstance(d, dict) and isinstance(d.get("state"), dict):
        return d["state"]
    return None


def bs_save(state):
    return bs_request("POST", {"state": state})


def bs_photo_put(photo):
    return bs_request("POST", {"photo": photo})


def write_heartbeat():
    """Heartbeat në bot-state (POST {heartbeat: ts}, auth x-bot-key: BOT_KEY).
    RREGULLUAR 2026-10-15 (Super Robot faza 2, rrënja B): më parë kjo thirrej
    VETËM nga run_jobs() brenda run_polling() — në webhook mode (shërbimi i
    vendosur: Render/Botkeep web service me PORT) run_jobs() s'ekzekutohet
    KURRË, ndaj heartbeat-i s'shkruhej kurrë dhe vlera në Blobs mbetej
    placeholder-i 1234567890 (2009). Tani e thërret edhe webhook mode."""
    return bs_request("POST", {"heartbeat": int(time.time())})


_HEARTBEAT_LAST = 0.0


def _heartbeat_due():
    """True kur kanë kaluar HEARTBEAT_EVERY sekonda nga heartbeat-i i fundit
    (zëvendëson every("heartbeat") të run_jobs() të hequr më 2026-11-19)."""
    global _HEARTBEAT_LAST
    now = time.time()
    if now - _HEARTBEAT_LAST >= HEARTBEAT_EVERY:
        _HEARTBEAT_LAST = now
        return True
    return False

# ---------------------------------------------------------------- state (memory + flush)
STATE = {}
STATE_DIRTY = False


def default_state():
    return {"offset": 0, "subs": [], "pending": {}, "digest_subs": [],
            "awaiting_search": [], "email_digest_subs": [],
            "pending_newsletter": {}}


def load_state():
    global STATE
    for attempt in range(12):
        s = bs_load()
        if s is not None:
            base = default_state()
            base.update(s)
            STATE = base
            print("State u ngarkua (offset=%s, subs=%d)." %
                  (STATE["offset"], len(STATE["subs"])), flush=True)
            return
        print("bot-state i paarritshëm, riprovo (%d)..." % (attempt + 1), flush=True)
        time.sleep(10)
    raise SystemExit("bot-state i paarritshëm pas 12 provash.")


def load_state_once():
    """Një tentativë e vetme pa retry/exit — për webhook (pa memorie të vjetruar)."""
    global STATE
    try:
        s = bs_load()
    except Exception:
        s = None
    if isinstance(s, dict):
        base = default_state()
        base.update(s)
        STATE = base
        return True
    return False


def save_state(force=False):
    global STATE_DIRTY
    if STATE_DIRTY or force:
        if bs_save(STATE):
            STATE_DIRTY = False
        else:
            print("  !! ruajtja e state-it dështoi (do riprovohet)", flush=True)


def mark_dirty():
    global STATE_DIRTY
    STATE_DIRTY = True

# ---------------------------------------------------------------- website data
DATA = {"products": None, "report": None, "history": {}, "ts": 0,
        "bykey": {}, "merged": [], "aliases": {}, "drops_by_key": {}}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cmimradar-bot/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def refresh_data(force=False):
    if not force and time.time() - DATA["ts"] < DATA_TTL and DATA["products"]:
        return True
    try:
        prods = fetch_json(SITE_URL + "/data/products.json")
        rep = fetch_json(SITE_URL + "/data/report.json")
        DATA["products"] = prods
        DATA["report"] = rep
        # historia: opsionale (nëse mungon, verdiktet dalin "Pa verdikt").
        try:
            DATA["history"] = fetch_json(SITE_URL + "/data/history.json") or {}
        except Exception:
            DATA["history"] = {}
        # katalogu i bashkuar (si faqja): 19.497 -> 19.370 grupe.
        merged, _n = radar_logic.merge_groups(prods.get("groups", []))
        DATA["merged"] = merged
        DATA["aliases"] = prods.get("aliases") or {}
        bykey = {g["key"]: g for g in merged}
        # aliaset: çelësat e vjetër → grupi aktual (alarmet e vjetra s'humbasin)
        for old, new in DATA["aliases"].items():
            if old not in bykey and new in bykey:
                bykey[old] = bykey[new]
        DATA["bykey"] = bykey
        # uljet e ditës sipas çelësit — për rreshtin "Ishte … (−…%)".
        DATA["drops_by_key"] = {d.get("key"): d for d in (rep.get("drops") or [])
                                if d.get("key")}
        DATA["ts"] = time.time()
        print("Të dhënat u rifreskuan (%d grupe të bashkuara)." % len(DATA["bykey"]),
              flush=True)
        return True
    except Exception as e:
        print("  !! refresh_data: %s" % str(e)[:150], flush=True)
        return bool(DATA["products"])

# ---------------------------------------------------------------- tekste & menu
MAIN_KB = {"keyboard": [
    [{"text": "🔍 Kërko"}, {"text": "🔥 Oferta e ditës"}],
    [{"text": "📋 Alarmet e mia"}, {"text": "📰 Digest"}],
    [{"text": "🤝 Dërgo ofertë"}, {"text": "❓ Ndihma"}],
], "resize_keyboard": True}

BOT_WELCOME = (
    "👋 <b>Përshëndetje! Unë jam boti i RADAR-it</b> 🤖\n\n"
    "🔍 Më shkruaj emrin e produktit dhe të them <b>çmimin më të lirë</b> në Shqipëri.\n"
    "🤖 Më pyet si <i>laptop për studentë nën 80 mijë</i> — të kthej vetëm produkte reale brenda buxhetit.\n"
    "🔔 Vendos <b>alarme uljeje</b> nga faqja — të njoftoj këtu sapo çmimi të bjerë.\n"
    "📸 Dërgomë <b>foto-ofertë</b> me <code>Produkti | Dyqani | Çmimi</code> — e publikojmë pas verifikimit.\n\n"
    "Përdor butonat më poshtë ose komandat:\n"
    "🔥 /oferta · 🏆 /top · 📰 /digest · 📧 /newsletter · 📋 /alerts · ❓ /help\n\n"
    "🌐 " + SITE_URL
)


BOT_COMMANDS = [
    {"command": "oferta", "description": "🔥 Oferta e ditës"},
    {"command": "top", "description": "🏆 Top 10 uljet e ditës"},
    {"command": "digest", "description": "📰 Digest-i ditor (abono/çabono)"},
    {"command": "newsletter", "description": "📧 Newsletter javor me email"},
    {"command": "alerts", "description": "📋 Alarmet e mia"},
    {"command": "help", "description": "❓ Ndihma"},
]


def register_commands():
    """Regjistron menunë e komandave te Telegrami (butoni '/' i chat-it).
    U shtua 2026-10-17: më parë komandat s'ishin regjistruar kurrë —
    përdoruesi s'i shihte te menuja, vetëm te tekstet."""
    r = tg("setMyCommands", {"commands": json.dumps(BOT_COMMANDS, ensure_ascii=False)})
    print("setMyCommands: %s" % bool(r and r.get("ok")), flush=True)


BOT_HELP = (
    "❓ <b>Ndihma — RADAR Bot</b>\n\n"
    "🔍 <b>Kërko:</b> shkruaj p.sh. <i>iphone 17</i> ose shtyp 🔍 Kërko.\n"
    "🤖 <b>Pyet Radarin:</b> shkruaj pyetje si <i>laptop për studentë nën 80 mijë</i> ose "
    "<i>kufje pa kabllo nën 10.000 lek</i> — buxheti është kufi i fortë, kthehen vetëm produkte reale.\n"
    "🔔 <b>Alarme:</b> nga faqja hap produktin → 🔔 <b>Alarm uljeje</b> → tabin <b>Telegram</b> → "
    "<b>Vazhdo</b> → <b>START</b> këtu. Fshihen me /alerts.\n"
    "🔥 <b>/oferta</b> — oferta e ditës.\n"
    "🏆 <b>/top</b> — top 10 uljet e ditës.\n"
    "📰 <b>/digest</b> — abonohu në top-5 uljet e ditës (çdo mbrëmje).\n"
    "📧 <b>/newsletter</b> — RADARI JAVOR me email (çdo të hënë).\n"
    "📸 <b>Ofertë nga komuniteti:</b> dërgo foto me <code>Produkti | Dyqani | Çmimi</code> në përshkrim.\n\n"
    "🌐 " + SITE_URL
)

COMMUNITY_INSTR = (
    "📤 <b>Dërgo ofertë nga komuniteti</b>\n\n"
    "1️⃣ Bëj <b>screenshot</b> të ofertës (Instagram, story, postim...)\n"
    "2️⃣ Dërgoja këtu si <b>fotografi</b> 📸\n"
    "3️⃣ Te përshkrimi i fotos shkruaj:\n"
    "<code>Produkti | Dyqani | Çmimi</code>\n"
    "Shembull: <i>iPhone 17 256GB | DyqanX | 145000</i>\n\n"
    "Publikohet në faqe <b>pas verifikimit</b>. Faleminderit! 🤝"
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def product_link(key):
    """Linku kanonik i produktit: /p/<slug>-<id> (si faqja).
    Bie mbrapa te /?p=<key> kur grupi s'gjendet."""
    g = DATA["bykey"].get(str(key))
    if g:
        return radar_logic.product_url(g)
    return SITE_URL + "/?p=" + urllib.parse.quote(str(key))


def user_subs(chat_id):
    return [s for s in STATE.get("subs", [])
            if s.get("channel") == "telegram" and str(s.get("chat_id")) == str(chat_id)]


def _fmt_pct(p):
    """Përqindje me 1 dhjetore, presje dhjetore shqipe: 15.6 -> '15,6'."""
    try:
        return ("%.1f" % float(p)).replace(".", ",")
    except Exception:
        return "—"


def trunc_word(s, n):
    """Pret titullin në <=n karaktere, në kufi fjale, me '…' kur pritet.

    (Bug /top 2026-11-20: titujt priteshin në mes të fjalës, p.sh.
    '…Full HD, Smart, i' / '…Techly I-SMART-GRAV, m', pa '…'.)
    Kthehet te hapësira e fundit brenda prerjes; hiqen shenjat e
    pikësimit në fund para '…'. Një fjalë e vetme më e gjatë se n
    pritet fort në n + '…'. Titujt <= n karaktere kthehen të paprekur.
    """
    s = s or ""
    if len(s) <= n:
        return s
    cut = s[:n]
    i = cut.rfind(" ")
    if i > 0:
        cut = cut[:i]
    cut = cut.rstrip(" \t,;:.-–—")
    return cut + "…"


def _canonical_cheapest(g):
    """(cheapest, cheapest_in_stock) — dy nivelet e stokut (§2.1).

    Parapëlqehen fushat e skanerit (group["best"] / ["best_in_stock"],
    radar_logic.scanner_cheapest) kur ekzistojnë DHE grupi s'është i
    bashkuar; "best" besohet VETËM kur grupi ka edhe "best_in_stock"
    (shenja që Part A e ripërcaktoi best-in si cheapest_valid — D4:
    boti lexonte group["cheapest"], të cilin skaneri s'e shkruan kurrë,
    ndaj rruga e skanerit s'ndizej kurrë). Për grupet e bashkuara
    (ofertat vijnë nga disa grupe burim) dhe kur fushat mungojnë / janë
    None, llogariten pastër me radar_logic. KURRË group["best"] i vjetër
    pa "best_in_stock" — ai mund të jetë ofertë pa stok (p.sh. REDMI
    NOTE 15 PRO+ 22.990 L).
    """
    if g.get("_merged"):
        return (radar_logic.cheapest_valid(g),
                radar_logic.cheapest_in_stock(g))
    best, bis = radar_logic.scanner_cheapest(g)
    ch = best if best is not None else radar_logic.cheapest_valid(g)
    ins = bis if bis is not None else radar_logic.cheapest_in_stock(g)
    return ch, ins


def _verdict_for_group(g, cheapest_price):
    """(label, reason) ose (None, None). Verdikti vjen vetëm nga historia.

    2026-11-23 (item 6): kur historia është e disponueshme (>= 2 pika),
    verdikti llogaritet me rregullin e botit (radar_logic.verdict_for,
    me family_best = group["family_best_in_stock"]) — fusha
    group["verdict"] e skanerit është një fotografi e shkruar nga
    buy_signal.py e skanimit të fundit dhe vjetrohet kur rregulli
    ndryshon (p.sh. iPhone 17 ishte BLI TANI me rregullin e vjetër,
    ndërsa historia e tij e sheshtë 74.000–74.900 thotë FLAT me
    rregullin e ri). Të dyja rrugët përdorin të njëjtin history.json
    dhe të njëjtin family_best_in_stock, ndaj sapo skaneri të shkruajë
    rregullin e ri, përgjigjet përkojnë prapë. Pa histori (ose < 2 pika)
    kthehet verdikti i skanerit me arsye gjenerike, si më parë.
    """
    key = g.get("key", "")
    hgroups = (DATA.get("history") or {}).get("groups", {}) or {}
    entry = hgroups.get(key) if isinstance(hgroups, dict) else None
    hist = (entry or {}).get("history") or []
    if hist and len(hist) >= 2 and cheapest_price:
        prices = [p for _d, p in hist if p]
        last30 = prices[-30:] if prices else []
        min30 = min(last30) if last30 else None
        med30 = statistics.median(last30) if last30 else None
        week_ago = hist[-8][1] if len(hist) > 7 else hist[0][1]
        chg7 = ((cheapest_price - week_ago) / week_ago * 100) if week_ago else 0
        label = radar_logic.verdict_for(
            hist, current=cheapest_price,
            family_best=g.get("family_best_in_stock"))
        if label is None and not g.get("_merged"):
            label = radar_logic.scanner_verdict(g)
        if label == "SHMANG":
            reason = "Çmimi u rrit %s%% kundrejt 7 ditëve më parë." % _fmt_pct(chg7)
        elif label == "BLI TANI":
            if min30 and med30:
                reason = ("Çmimi sot është te minimumi 30-ditor (%s L) dhe "
                          "≥5%% nën medianën 30-ditore (%s L)."
                          % (fmt_price(min30), fmt_price(med30)))
            else:
                reason = "Çmimi sot është ndër më të ulëtit historik."
        elif label == "FLAT":
            # A3 (site rule): histori e sheshtë — çmimi s'ka lëvizur kurrë,
            # ndaj s'ka bazë për BLI TANI. Shfaqet si "Çmim i qëndrueshëm"
            # (e njëjta etiketë gri si faqja), jo si verdikt blerjeje.
            return ("Çmim i qëndrueshëm",
                    "Çmimi nuk ka lëvizur në historinë e regjistruar.")
        elif label == "PRIT":
            if min30 and cheapest_price <= min30:
                reason = ("Çmimi sot është te minimumi 30-ditor (%s L), "
                          "por jo ≥5%% nën medianën 30-ditore (%s L) — "
                          "prandaj PRIT, jo BLI TANI."
                          % (fmt_price(min30), fmt_price(med30)))
            elif min30:
                reason = ("Minimumi 30-ditor është %s L — sot %s L."
                          % (fmt_price(min30), fmt_price(cheapest_price)))
            else:
                reason = "Çmimi nuk është te minimumi historik."
        else:
            return None, None
        return label, reason
    if not g.get("_merged"):
        label = radar_logic.scanner_verdict(g)
        if label:
            if label == "FLAT":
                return ("Çmim i qëndrueshëm",
                        "Çmimi nuk ka lëvizur në historinë e regjistruar.")
            generic = {"BLI TANI": "Çmimi është ndër më të ulëtit historik.",
                       "PRIT": "Çmimi nuk është te minimumi historik.",
                       "SHMANG": "Çmimi është rritur ndjeshëm së fundmi."}
            return label, generic.get(label, "")
    return None, None


_VERDICT_PILL = {"BLI TANI": "🟢", "PRIT": "🟡", "SHMANG": "🔴",
                   "Çmim i qëndrueshëm": "⚪"}

# ---------------------------------------------------------------- kërkimi
def search_products(query, limit=5):
    """Kërkimi i përbashkët me faqen, mbi grupet e BASHKUARA:
    sinonime, fraza, numra të detyrueshëm (ps5 ≠ ps4), përjashtim
    aksesorësh kur pyetja emëron një klasë pajisjeje; renditja: përputhje
    kategorie, pastaj fillim titulli, pastaj çmimi më i lirë."""
    if not DATA.get("merged"):
        return []
    return [g for g, _c in radar_logic.search_ranked(
        DATA["merged"], query, DATA.get("aliases"), limit=limit)]


def format_product(g, key):
    cheapest, in_stock = _canonical_cheapest(g)
    comp = radar_logic.comparable_offers(g)
    lines = ["🔎 <b>%s</b>" % trunc_word(g.get("title", ""), 90)]
    if cheapest and cheapest.get("price"):
        price = cheapest["price"]
        store = cheapest.get("store_name", "")
        stock_note = ""
        if cheapest.get("in_stock") is None:
            stock_note = " (stoku i pakonfirmuar)"
        lines.append("💰 <b>%s L</b> te <b>%s</b> · Më i liri%s" %
                     (fmt_price(price), store, stock_note))
        if (in_stock and in_stock.get("price")
                and (in_stock.get("store") != cheapest.get("store")
                     or in_stock.get("price") != price)):
            lines.append("Më i liri në stok: <b>%s L</b> te <b>%s</b>" %
                         (fmt_price(in_stock["price"]),
                          in_stock.get("store_name", "")))
        if len(comp) > 1:
            mx = max(o.get("price", 0) for o in comp)
            if mx and mx > price:
                lines.append("📊 %d dyqane · diferenca %s%%" %
                             (len(comp), _fmt_pct((mx - price) / mx * 100)))
        # etiketat: çmim i dyshimtë / vitrinë / i përdorur (ku zbatohen)
        med = radar_logic.group_median(g)
        offers = g.get("offers", [])
        if any(radar_logic.is_suspicious(o, med) for o in offers):
            lines.append("⚠️ Çmim i dyshimtë")
        if any(radar_logic.is_vitrine(o, g.get("title", "")) for o in offers):
            lines.append("🏷️ Vitrinë")
        if (any(radar_logic.is_used(o, g.get("title", "")) for o in offers)
                or g.get("condition") == "used"):
            lines.append("♻️ I përdorur")
        # "Ishte …" — VETËM nga report drops (prev i djeshëm), kurrë nga old_price.
        drop = DATA.get("drops_by_key", {}).get(key)
        if drop and drop.get("prev") and drop["prev"] > price:
            pct = drop.get("pct")
            if pct is None:
                pct = radar_logic.discount_pct(price, drop["prev"])
            lines.append("📉 Ishte %s L (−%s%%)" %
                         (fmt_price(drop["prev"]), _fmt_pct(pct)))
        label, reason = _verdict_for_group(g, price)
        if label:
            lines.append("%s <b>%s</b> — %s" %
                         (_VERDICT_PILL.get(label, "⚪"), label, reason))
        else:
            lines.append("Pa verdikt · pa histori")
        if cheapest.get("url"):
            lines.append('🛒 <a href="%s">Bli te %s ↗</a>' %
                         (htmlmod.escape(cheapest["url"]), store))
    else:
        lines.append("⏳ Pa çmime ende në radar.")
    lines.append('🔗 <a href="%s">Shiko në faqe</a>' % product_link(key))
    return "\n".join(lines)


def handle_search(chat_id, query):
    if not refresh_data():
        tg_send(chat_id, "⚠️ S'munda të lexoj të dhënat e faqes. Provo pas pak.")
        return
    res = search_products(query)
    if not res:
        tg_send(chat_id,
                "🔍 S'gjeta <b>%s</b> në radar.\n\nProvo me më pak fjalë (p.sh. <i>iphone 17</i>) "
                "ose shiko faqen: %s" % (htmlmod.escape(query[:60]), SITE_URL))
        return
    g = res[0]
    tg_send(chat_id, format_product(g, g["key"]), preview=True)
    if len(res) > 1:
        btns = [[{"text": ("✅ " if i == 0 else "") + trunc_word(r["title"], 40),
                  # F-6.1 (2026-11-20): pronari (chat-i që e nisi kërkimin)
                  # shkruhet në callback_data — handle_callback e verifikon.
                  "callback_data": "prod:%s:%s" % (r["key"], chat_id)}] for i, r in enumerate(res[1:5])]
        tg_send(chat_id, "🔎 <b>Rezultate të tjera:</b>", markup={"inline_keyboard": btns})


# ---------------------------------------------------------------- "Pyet Radarin" (AI)
_AI_RE = re.compile(
    r"më i liri|ku është më lirë|cheapest|best|sa kushton|cili|cila|"
    r"nën|deri|under|për student|student", re.IGNORECASE)


def _fx():
    """(rate, date) e kursit EUR→LEK nga report.json i ngarkuar, ose
    (None, None) kur të dhënat s'janë ngarkuar ende. Kurrë kurs i shpikur."""
    rep = DATA.get("report") or {}
    fx = (rep.get("fx") or {}).get("EUR_ALL")
    date = (rep.get("fx") or {}).get("date")
    try:
        return (float(fx), date) if fx else (None, None)
    except (TypeError, ValueError):
        return None, None


def _budget_line(res, lang):
    """'800 € (≈72.094 L · kursi 90,12 · 2026-11-20)' kur buxheti erdhi nga
    EUR (V7: kursi citohet gjithmonë); None për buxhet në lekë/pa buxhet."""
    if not res.get("budget_eur") or not res.get("fx") or not res.get("budget"):
        return None
    dec = "," if lang == "sq" else "."
    word = "kursi" if lang == "sq" else "rate"
    rate_s = ("%.2f" % res["fx"]).replace(".", dec)
    date_s = (" · " + res["fx_date"]) if res.get("fx_date") else ""
    return "%s € (≈%s L · %s %s%s)" % (
        fmt_price(res["budget_eur"]), fmt_price(res["budget"]),
        word, rate_s, date_s)


def _is_ai_query(txt):
    """Pyetje me buxhet OSE me model pyetjeje → answer_ai(); përndryshe
    kërkim me fjalë kyçe (top 5)."""
    fx, _date = _fx()
    if radar_logic.parse_budget(txt, fx=fx) is not None:
        return True
    return bool(_AI_RE.search(txt or ""))


def handle_ai(chat_id, query):
    if not refresh_data():
        tg_send(chat_id, "⚠️ S'munda të lexoj të dhënat e faqes. Provo pas pak.")
        return
    fx, fx_date = _fx()
    res = radar_logic.answer_ai(DATA["merged"], query, DATA.get("aliases"),
                                fx=fx, fx_date=fx_date)
    lang = res["lang"]
    picks = res["picks"]
    if picks:
        header = ("🤖 <b>RADAR — përgjigjja</b>\n" if lang == "sq"
                  else "🤖 <b>RADAR — answer</b>\n")
        lines = [header]
        bl = _budget_line(res, lang)
        if bl:
            lines.append(("Buxheti: " if lang == "sq" else "Budget: ") + bl + "\n")
        for i, p in enumerate(picks, 1):
            g, offer = p["group"], p["offer"]
            stock = ""
            if offer.get("in_stock") is True:
                stock = (" · në stok" if lang == "sq"
                         else " · in stock")
            elif offer.get("in_stock") is None:
                stock = (" · stoku i pakonfirmuar" if lang == "sq"
                         else " · stock unconfirmed")
            label, _reason = _verdict_for_group(g, offer["price"])
            lines.append("<b>%d. %s</b>" % (i, trunc_word(g.get("title", ""), 90)))
            lines.append("💰 <b>%s L</b> te <b>%s</b>%s" %
                         (fmt_price(offer["price"]),
                          offer.get("store_name", ""), stock))
            lines.append(p["reason"])
            if label:
                lines.append("%s <b>%s</b>" %
                             (_VERDICT_PILL.get(label, "⚪"), label))
            lines.append('🔗 <a href="%s">Shiko</a>' %
                         radar_logic.product_url(g))
            lines.append("")
        tg_send(chat_id, "\n".join(lines).strip(), preview=True)
        return
    fb = res["fallback"]
    budget = radar_logic.parse_budget(query, fx=fx)
    if fb:
        g, offer = fb["group"], fb["offer"]
        btxt = (_budget_line(res, lang)
                or (("%s L" % fmt_price(budget)) if budget else "—"))
        # §6: same stock text as the picks (confirmed stock is named, too)
        if offer.get("in_stock") is True:
            stock = (" · në stok" if lang == "sq" else " · in stock")
        elif offer.get("in_stock") is None:
            stock = (" · stoku i pakonfirmuar" if lang == "sq"
                     else " · stock unconfirmed")
        else:
            stock = ""
        if lang == "en":
            text = ("Nothing under <b>%s</b> — the cheapest real option is:\n\n"
                    "<b>%s</b>\n💰 <b>%s L</b> at <b>%s</b>%s\n"
                    '🔗 <a href="%s">View</a>' %
                    (btxt,
                     trunc_word(g.get("title", ""), 90), fmt_price(offer["price"]),
                     offer.get("store_name", ""), stock, radar_logic.product_url(g)))
        else:
            text = ("Nuk gjeta asgjë nën <b>%s</b>. Më i liri real është:\n\n"
                    "<b>%s</b>\n💰 <b>%s L</b> te <b>%s</b>%s\n"
                    '🔗 <a href="%s">Shiko</a>' %
                    (btxt,
                     trunc_word(g.get("title", ""), 90), fmt_price(offer["price"]),
                     offer.get("store_name", ""), stock, radar_logic.product_url(g)))
        tg_send(chat_id, text, preview=True)
        return
    if lang == "en":
        tg_send(chat_id,
                "I couldn't find real products for that request.\n\n"
                "Try fewer words (e.g. <i>iphone 17</i>) or check the site: %s"
                % SITE_URL)
    else:
        tg_send(chat_id,
                "Nuk gjeta produkte reale për këtë kërkesë.\n\n"
                "Provo me më pak fjalë (p.sh. <i>iphone 17</i>) "
                "ose shiko faqen: %s" % SITE_URL)


def handle_free_text(chat_id, txt):
    if _is_ai_query(txt):
        handle_ai(chat_id, txt)
    else:
        handle_search(chat_id, txt)

# ---------------------------------------------------------------- komandat
def cmd_oferta(chat_id):
    if not refresh_data():
        tg_send(chat_id, "⚠️ S'munda të lexoj të dhënat. Provo pas pak.")
        return
    rep = DATA["report"] or {}
    dod = rep.get("deal_of_day") or {}
    if not dod:
        tg_send(chat_id, "🔥 S'ka ofertë të ditës sot. Kthehu nesër!")
        return
    name = trunc_word(dod.get("name", ""), 90)
    # RREGULLUAR 2026-11-19 (bot-sync, bug 1): deal_of_day NUK ka fushë
    # "price" — çmimi është "new_price", i vjetri "old_price" (prev),
    # ulja "pct". Më parë asnjë rresht çmimi s'dilnte kurrë.
    price = dod.get("new_price")
    store = dod.get("store_name", "")
    old = dod.get("old_price")
    pct = dod.get("pct")
    key = dod.get("key", "")
    lines = ["🔥 <b>OFERTA E DITËS</b>\n", "🏷️ <b>%s</b>" % name]
    if price:
        lines.append("💰 <b>%s L</b> te <b>%s</b>" % (fmt_price(price), store))
    if old and price and old > price:
        if pct is None:
            pct = radar_logic.discount_pct(price, old)
        lines.append("📉 Ishte %s L (−%s%%)" %
                     (fmt_price(old), _fmt_pct(pct)))
    if key:
        lines.append('🔗 <a href="%s">Shiko në faqe</a>' % product_link(key))
    tg_send(chat_id, "\n".join(lines), preview=True)


def cmd_digest(chat_id):
    subs = STATE.setdefault("digest_subs", [])
    if str(chat_id) in [str(x) for x in subs]:
        STATE["digest_subs"] = [x for x in subs if str(x) != str(chat_id)]
        mark_dirty()
        record_op({"digest_toggle": {"chat_id": str(chat_id), "on": False}})
        tg_send(chat_id, "📰 <b>Digest-i u çaktivizua.</b>\nNuk do të marrësh më top-5 uljet e ditës.")
    else:
        subs.append(chat_id)
        mark_dirty()
        record_op({"digest_toggle": {"chat_id": str(chat_id), "on": True}})
        tg_send(chat_id,
                "📰 <b>U abonove në digest-in ditor!</b>\n\nÇdo mbrëmje të vijnë "
                "<b>top-5 uljet e ditës</b> automatikisht. Për ta ndalur: /digest sërish.")


def cmd_newsletter(chat_id):
    """Abonim/ç'abonim në newsletter-in email javor."""
    if str(chat_id) in STATE.get("pending_newsletter", {}):
        tg_send(chat_id, "📧 Shkruaj email-in tënd për newsletter-in javor.")
        return
    subs = STATE.setdefault("email_digest_subs", [])
    # kontrollo nëse ky chat ka tashmë email të abonuar
    existing = [s for s in subs if isinstance(s, dict) and str(s.get("chat_id")) == str(chat_id)]
    if existing:
        STATE["email_digest_subs"] = [s for s in subs if not (isinstance(s, dict) and str(s.get("chat_id")) == str(chat_id))]
        mark_dirty()
        record_op({"newsletter_del": {"chat_id": str(chat_id)}})
        tg_send(chat_id, "📧 <b>U ç'abonove nga newsletter-i.</b>\nNuk do të marrësh më RADARIN JAVOR me email.")
        return
    STATE.setdefault("pending_newsletter", {})[str(chat_id)] = True
    mark_dirty()
    record_op({"pending_newsletter_put": str(chat_id)})
    tg_send(chat_id, "📧 <b>Newsletter javor</b>\n\nShkruaj email-in tënd dhe çdo të hënë do të marrësh <b>RADARIN JAVOR</b> me top uljet e javës.")


def cmd_top(chat_id):
    if not refresh_data():
        tg_send(chat_id, "⚠️ S'munda të lexoj të dhënat. Provo pas pak.")
        return
    rep = DATA["report"] or {}
    drops = rep.get("drops") or []
    if not drops:
        tg_send(chat_id, "📊 S'ka ulje sot. Kthehu nesër!")
        return
    # RREGULLUAR 2026-11-19 (bot-sync, bug 2): pct/prev merren nga report-i
    # (ulja ditë-me-ditë). KURRË nuk rillogaritet nga store old_price —
    # për Maus DM210 jepte −33,3% në vend të −15,6%.
    lines = ["🏆 <b>TOP 10 ULJET E DITËS</b>\n"]
    for i, d in enumerate(drops[:10], 1):
        title = trunc_word(d.get("title") or "", 55)
        price = d.get("price")
        store = d.get("store_name", "")
        pct = d.get("pct")
        if pct is None and d.get("prev") and price:
            pct = radar_logic.discount_pct(price, d["prev"])
        pct_txt = (" (−%s%%)" % _fmt_pct(pct)) if pct is not None else ""
        lines.append("%d. <b>%s</b>\n   💰 %s L%s te %s" % (
            i, title, fmt_price(price), pct_txt, store))
    tg_send(chat_id, "\n".join(lines))


def send_alerts_list(chat_id):
    al = user_subs(chat_id)
    if not al:
        tg_send(chat_id,
                "📋 <b>S'ke alarme aktive.</b>\n\nVendos të reja nga faqja: "
                "hap produktin → 🔔 <b>Alarm uljeje</b> → tabin <b>Telegram</b>.\n🌐 " + SITE_URL)
        return
    lines = ["📋 <b>Alarmet e tua aktive (%d):</b>\n" % len(al)]
    btns = []
    for s in al[:20]:
        lines.append("• <b>%s</b>\n  synim ≤ <b>%s L</b>" % (trunc_word(s["name"], 60), fmt_price(s["target"])))
        btns.append([{"text": "❌ %s" % trunc_word(s["name"], 28),
                      "callback_data": "del:%s:%d" % (s["key"], s["target"])}])
    tg_send(chat_id, "\n".join(lines), markup={"inline_keyboard": btns})


def handle_callback(cb):
    cb_id = cb.get("id", "")
    data = cb.get("data") or ""
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    if not chat_id:
        return
    if data.startswith("del:"):
        _, key, target = (data.split(":") + ["", ""])[:3]
        before = STATE.get("subs", [])
        removed = [s for s in before
                   if s.get("channel") == "telegram"
                   and str(s.get("chat_id")) == str(chat_id)
                   and s.get("key") == key and str(s.get("target")) == target]
        STATE["subs"] = [s for s in before if s not in removed]
        mark_dirty()
        if removed:
            record_op({"sub_del": removed})
        tg_answer_callback(cb_id, "✅ Alarmi u fshi." if removed else "S'u gjet.")
        if removed:
            tg_send(chat_id, "🗑️ <b>Alarmi u fshi.</b>\nMund të vendosësh të ri nga faqja kur të duash. 🔔")
    elif data.startswith("prod:"):
        parts = data.split(":")
        key = parts[1] if len(parts) > 1 else ""
        owner = parts[2] if len(parts) > 2 else ""
        # F-6.1 (2026-11-20): callback-i i përket vetëm chat-it që e nisi
        # kërkimin (pronari shkruhet në callback_data nga handle_search).
        # Format i vjetër "prod:<key>" (pa pronar, dërguar para fix-it)
        # pranohet — fail-open vetëm për butona të vjetër; me pronar të
        # shënuar, një chat tjetër refuzohet (butoni i përcjellë s'punon).
        if owner and owner != str(chat_id):
            tg_answer_callback(cb_id, "Ky buton i përket një bisede tjetër.")
            return
        g = DATA["bykey"].get(key)
        tg_answer_callback(cb_id)
        if g:
            tg_send(chat_id, format_product(g, key), preview=True)
    else:
        tg_answer_callback(cb_id)


def handle_deeplink(chat_id, payload):
    """tg_<key>_<target> / em_<key>_<target> / community"""
    if payload == "community":
        tg_send(chat_id, COMMUNITY_INSTR)
        return
    mm = re.match(r"^(tg|em)_([0-9a-f]{16})_(\d+)$", payload)
    if not mm:
        tg_send(chat_id, BOT_WELCOME, markup=MAIN_KB)
        return
    kind, key, target = mm.group(1), mm.group(2), int(mm.group(3))
    if not refresh_data():
        tg_send(chat_id, "⚠️ S'munda të lexoj të dhënat. Provo pas pak.")
        return
    g = DATA["bykey"].get(key)
    if not g:
        tg_send(chat_id, "❌ Produkti nuk u gjet në radar. Provo sërish nga faqja.")
        return
    if kind == "tg":
        subs = STATE.setdefault("subs", [])
        new_sub = {"channel": "telegram", "chat_id": chat_id, "key": key,
                   "target": target, "name": trunc_word(g["title"], 80)}
        exists = any(s.get("channel") == "telegram"
                     and str(s.get("chat_id")) == str(chat_id)
                     and s.get("key") == key and s.get("target") == target
                     for s in subs)
        if not exists:
            subs.append(new_sub)
            mark_dirty()
            record_op({"sub_add": new_sub})
        cheapest, _ins = _canonical_cheapest(g)
        now_txt = ("Çmimi tani: %s L te %s." % (
            fmt_price(cheapest["price"]), cheapest.get("store_name", ""))
            if cheapest and cheapest.get("price") else "")
        tg_send(chat_id,
                "✅ <b>Alarmi u aktivizua!</b>\n\n<b>%s</b>\n"
                "Do të njoftohesh këtu kur çmimi të bjerë në <b>%s L</b> ose më pak.\n\n" % (
                    trunc_word(g["title"], 80), fmt_price(target)) +
                ("Ky alarm ekzistonte tashmë — nuk u dyfishua." if exists else now_txt),
                markup=MAIN_KB)
    else:
        pend = {"key": key, "target": target, "name": trunc_word(g["title"], 80)}
        STATE.setdefault("pending", {})[str(chat_id)] = pend
        mark_dirty()
        record_op({"pending_put": {"chat_id": str(chat_id), "data": pend}})
        tg_send(chat_id,
                "📧 <b>%s</b>\n\nShkruaj <b>email-in tënd</b> si mesazh këtu që ta "
                "konfirmojmë alarmin (synimi: %s L)." % (trunc_word(g["title"], 80), fmt_price(target)))


def handle_community_photo(chat_id, m):
    photos = m.get("photo") or []
    if not photos:
        return False
    cap = (m.get("caption") or "").strip()
    parts = [p.strip() for p in cap.split("|")]
    product = parts[0] if len(parts) > 0 and parts[0] else ""
    store = parts[1] if len(parts) > 1 else ""
    price = None
    if len(parts) > 2:
        pm = re.search(r"[\d.,\s]+", parts[2])
        if pm:
            digits = re.sub(r"[^\d]", "", pm.group(0))
            price = int(digits) if digits else None
    # F-6.2 (2026-11-20): foto pa emër produkti (caption bosh, ose caption
    # që nis me "|" — p.sh. "| Dyqani | 100") NUK ruhet si "(pa emër)".
    # Refuzohet me udhëzimin e formatit, para shkarkimit/ruajtjes; kthehet
    # True që handle_message të mos bjerë në trajtimin e tekstit.
    if not product:
        tg_send(chat_id,
                "📸 Foto pa emër produkti — dërgoje sërish me përshkrimin:\n\n"
                "<code>Produkti | Dyqani | Çmimi</code>\n"
                "Shembull: <i>iPhone 17 256GB | DyqanX | 145000</i>")
        return True
    fid = photos[-1]["file_id"]
    fpath = tg_get_file(fid)
    data = tg_download_file(fpath) if fpath else None
    pid = "c%d_%04d" % (int(time.time()), random.randint(0, 9999))
    ok = bool(bs_photo_put({
        "id": pid, "chat_id": chat_id, "product": product, "store": store,
        "price": price, "caption": cap, "date": time.strftime("%Y-%m-%d %H:%M"),
        "status": radar_logic.STATUS_PENDING,
        "data_b64": base64.b64encode(data).decode() if data else ""}))
    tg_send(chat_id,
            ("📸 <b>Oferta u mor!</b>\n\n" if ok else "📸 <b>Oferta u mor, por fotoja s'u ruajt!</b>\n\n") +
            "<b>%s</b>%s%s\n\n"
            "Statusi: <b>në pritje</b> — do ta verifikojmë dhe publikojmë "
            "te “Oferta nga komuniteti” sa më shpejt. Faleminderit! 🤝" %
            (product or "(pa emër)",
             " — " + store if store else "",
             " — " + fmt_price(price) + " L" if price else ""))
    return True

# ---------------------------------------------------------------- shpërndarësi i mesazheve
def handle_message(m):
    chat_id = m.get("chat", {}).get("id")
    if not chat_id:
        return
    if handle_community_photo(chat_id, m):
        return
    txt = (m.get("text") or "").strip()
    if not txt:
        return
    low = txt.lower()

    # kërkim në pritje (pas butonit 🔍 Kërko)
    if str(chat_id) in [str(x) for x in STATE.get("awaiting_search", [])]:
        STATE["awaiting_search"] = [x for x in STATE["awaiting_search"] if str(x) != str(chat_id)]
        mark_dirty()
        record_op({"awaiting_del": str(chat_id)})
        handle_free_text(chat_id, txt)
        return

    # butonat e menysë
    if txt == "🔍 Kërko":
        if str(chat_id) not in [str(x) for x in STATE.get("awaiting_search", [])]:
            STATE.setdefault("awaiting_search", []).append(chat_id)
            mark_dirty()
            record_op({"awaiting_add": str(chat_id)})
        tg_send(chat_id, "🔍 <b>Çfarë po kërkon?</b>\nShkruaj emrin e produktit, p.sh. <i>iphone 17</i>.")
        return
    if txt == "🔥 Oferta e ditës":
        cmd_oferta(chat_id)
        return
    if txt == "📋 Alarmet e mia":
        send_alerts_list(chat_id)
        return
    if txt in ("📰 Digest",):
        cmd_digest(chat_id)
        return
    if txt == "🤝 Dërgo ofertë":
        tg_send(chat_id, COMMUNITY_INSTR)
        return
    if txt == "❓ Ndihma":
        tg_send(chat_id, BOT_HELP)
        return

    # komandat
    if low in ("/help", "ndihme", "ndihmë"):
        tg_send(chat_id, BOT_HELP)
        return
    if low in ("/alerts", "/alertat", "/alarmet", "alarmet e mia"):
        send_alerts_list(chat_id)
        return
    if low == "/oferta":
        cmd_oferta(chat_id)
        return
    if low == "/digest":
        cmd_digest(chat_id)
        return
    if low in ("/top", "/top10", "top"):
        cmd_top(chat_id)
        return
    if low in ("/newsletter", "/email"):
        cmd_newsletter(chat_id)
        return
    if low.startswith("/stop"):
        tg_send(chat_id, "🛑 Për të ndaluar njoftimet, fshi alarmet me /alerts. "
                         "Bllokimi i botit i ndalon të gjitha.")
        return
    if txt == "/start" or txt.startswith("/start "):
        parts = txt.split(None, 1)
        payload = parts[1] if len(parts) > 1 else ""
        if payload:
            handle_deeplink(chat_id, payload)
        else:
            tg_send(chat_id, BOT_WELCOME, markup=MAIN_KB)
        return

    # email në pritje (newsletter javor)
    if str(chat_id) in STATE.get("pending_newsletter", {}):
        if EMAIL_RE.match(txt):
            rec = {"email": txt, "chat_id": str(chat_id)}
            STATE.setdefault("email_digest_subs", []).append(rec)
            del STATE["pending_newsletter"][str(chat_id)]
            mark_dirty()
            record_op({"newsletter_put": rec})
            record_op({"pending_newsletter_del": str(chat_id)})
            tg_send(chat_id,
                    "✅ <b>U abonove në newsletter!</b>\n\nÇdo të hënë do të marrësh "
                    "<b>RADARIN JAVOR</b> te <b>%s</b>.\nPër ta ndalur: /newsletter sërish." % txt,
                    markup=MAIN_KB)
        else:
            tg_send(chat_id, "❌ Ky s'duket email i vlefshëm. Shkruaje sërish, p.sh. emri@shembull.com")
        return

    # email në pritje (konfirmim alarmi)
    if str(chat_id) in STATE.get("pending", {}):
        p = STATE["pending"][str(chat_id)]
        if EMAIL_RE.match(txt):
            new_sub = {"channel": "email", "email": txt, "key": p["key"],
                       "target": p["target"], "name": p["name"]}
            STATE.setdefault("subs", []).append(new_sub)
            del STATE["pending"][str(chat_id)]
            mark_dirty()
            record_op({"sub_add": new_sub})
            record_op({"pending_del": [str(chat_id)]})
            tg_send(chat_id,
                    "✅ <b>Alarmi me email u aktivizua!</b>\n\nNjoftimet do të vijnë te "
                    "<b>%s</b> kur çmimi të bjerë në %s L ose më pak." % (txt, fmt_price(p["target"])),
                    markup=MAIN_KB)
        else:
            tg_send(chat_id, "❌ Ky s'duket email i vlefshëm. Shkruaje sërish, p.sh. emri@shembull.com")
        return

    # tekst i lirë → "Pyet Radarin" (me buxhet/pyetje) ose kërkim me fjalë kyçe
    handle_free_text(chat_id, txt)


def handle_update(u):
    try:
        if u.get("callback_query"):
            handle_callback(u["callback_query"])
            return
        m = u.get("message", {})
        if m:
            handle_message(m)
    except Exception:
        print("  !! gabim në update %s:\n%s" %
              (u.get("update_id"), traceback.format_exc()[:500]), flush=True)

# ---------------------------------------------------------------- main loop
# SHËNIM 2026-11-19 (bot-sync, spec §4): alarmet e çmimeve, alarmet push,
# push digest, digest-i ditor dhe admin summary DËRGOHEN VETËM nga cron-i
# (check_remote_alerts.py). Kopjet e fjetura që ishin këtu — run_jobs(),
# check_price_alerts(), check_push_alerts(), send_push_digest(),
# send_daily_digest(), admin_summary() (+ send_email, push_blobs_get/delete,
# webpush_send) — u hoqën: në webhook mode (produksion) nuk ekzekutoheshin
# kurrë, dhe dyfishimi ishte burim gabimesh (tekste/URL/format të ndryshëm).
# Këtu mbetet vetëm heartbeat-i (e lexon health_check C-BOT-2).
def run_polling():
    """Long polling — për testim lokal. Thërret VETËM heartbeat-in."""
    while True:
        try:
            PENDING_OPS.clear()  # në polling ruhet full-state, jo ops-e
            params = {"timeout": 30}
            if STATE.get("offset"):
                params["offset"] = STATE["offset"]
            ups = tg("getUpdates", params, timeout=45) or {}
            for u in ups.get("result", []):
                STATE["offset"] = max(STATE.get("offset", 0), u["update_id"] + 1)
                handle_update(u)
            mark_dirty()
            save_state()
        except Exception:
            print("  !! gabim në poll:\n%s" % traceback.format_exc()[:600], flush=True)
            time.sleep(5)
        if _heartbeat_due():
            write_heartbeat()


# ---------------------------------------------------------------- ops (intent-tracking)
# Çdo ndryshim i state-it regjistrohet si operacion atomik; në webhook mode
# dërgohen VETËM ops-et (kurrë mbishkrim i plotë) → pa gara me Blobs.
PENDING_OPS = []


def record_op(op):
    PENDING_OPS.append(op)


def flush_ops():
    # Swap atomik (nën GIL): merr listën aktuale dhe lë një të zbrazët në vend.
    # Më parë: iterate + .clear() mbi TË NJËJTËN listë globale — një op i
    # regjistruar nga një thread tjetër GJATË flush-it pastrohej pa u dërguar
    # (humbje e heshtur). Me swap-in, op-et e reja mbesin në listën e re për
    # flush-in tjetër; çdo op dërgohet saktësisht një herë.
    global PENDING_OPS
    ops, PENDING_OPS = PENDING_OPS, []
    ok = True
    failed = []
    for op in ops:
        if bs_request("POST", op) is None:
            ok = False
            failed.append(op)
    if failed:
        # RREGULLUAR 2026-10-17: op-et e dështuara (bot-state poshtë për një
        # çast) më parë HUMBNIN përgjithmonë; tani rivihen në krye të radhës
        # për flush-in tjetër (kapak 200 — hidhen më të vjetrat).
        PENDING_OPS = (failed + PENDING_OPS)[:200]
        print("  !! flush_ops: %d op-e u ruajtën për riprovë" % len(failed), flush=True)
    return ok


def run_webhook():
    """Webhook mode — për Render Web Service (falas). Vetëm stdlib."""
    import hashlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    secret = hashlib.sha256(TG_BOT_TOKEN.encode()).hexdigest()[:32]
    secret_path = "/webhook/" + secret
    # secret_token i Telegram-it (mbrojtje shtesë: verifikohet header-i)
    secret_token = hashlib.sha256((TG_BOT_TOKEN + ":wh-secret").encode()).hexdigest()[:32]
    MAX_BODY = 1024 * 1024  # 1 MB — update-et e Telegram janë disa KB

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/" or self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path != secret_path:
                self.send_response(403)
                self.end_headers()
                return
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret_token:
                self.send_response(403)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length > MAX_BODY:
                self.send_response(413)
                self.end_headers()
                return
            try:
                u = json.loads(self.rfile.read(length) or b"{}")
                if u:
                    # ops-et dërgohen atomike; s'ka mbishkrim të plotë → pa gara
                    PENDING_OPS.clear()
                    load_state_once()
                    handle_update(u)
                    flush_ops()
                    # heartbeat oportunist: çdo update provon gjallërinë
                    if _heartbeat_due():
                        write_heartbeat()
            except Exception:
                print("  !! gabim në webhook:\n%s" % traceback.format_exc()[:400], flush=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    # regjistro webhook-un te Telegram (URL nga Render)
    base = (os.environ.get("PUBLIC_BASE_URL")
            or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
    if base:
        wh_url = base + secret_path
        register_commands()
        r = tg("setWebhook", {"url": wh_url, "max_connections": 40,
                              "secret_token": secret_token,
                              "allowed_updates": ["message", "callback_query"]})
        print("setWebhook: %s -> %s" % (wh_url, bool(r and r.get("ok"))), flush=True)
    else:
        print("RENDER_EXTERNAL_URL mungon — webhook s'u regjistrua.", flush=True)

    port = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Webhook server në portin %d." % port, flush=True)
    # Në webhook mode NUK ka jobs loop të plotë: kontrollet periodike
    # (alarme, digest, përmbledhje admin) i bën cron-i i VM-së. POR heartbeat-i
    # PO: një fije daemon e shkruan çdo HEARTBEAT_EVERY sa kohë procesi është
    # zgjuar (rrënja B, 2026-10-15) + heartbeat oportunist në çdo update (lart).
    # Kujdes: kur host-i falas e fle shërbimin, fija fle bashkë me procesin —
    # heartbeat-i ndalon derisa një mesazh/webhook e zgjon sërish.
    import threading

    def _heartbeat_loop():
        while True:
            try:
                write_heartbeat()
            except Exception:
                pass
            time.sleep(HEARTBEAT_EVERY)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    print("RADAR Bot worker u nis.", flush=True)
    load_state()
    refresh_data(force=True)
    me = tg("getMe", {})
    print("Bot: %s" % ((me.get("result") or {}).get("username") if me else "?"), flush=True)
    if not (os.environ.get("PORT") or os.environ.get("SERVER_PORT")):
        register_commands()
    if os.environ.get("PORT") or os.environ.get("SERVER_PORT"):
        # webhook mode: pa mesazh "u ndez" (cold start-et janë të shpeshta)
        run_webhook()
    else:
        if ADMIN_CHAT:
            tg_send(ADMIN_CHAT, "🤖 <b>RADAR Bot u ndez.</b>\nPërgjigjet në sekonda. ✅")
        run_polling()


if __name__ == "__main__":
    main()
