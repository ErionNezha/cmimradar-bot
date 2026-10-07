#!/usr/bin/env python3
"""ÇmimRadar Bot — Render Background Worker (long polling 24/7).

Përgjigjet në sekonda ("si robot"), i lidhur me website-in live
(lexon products.json/report.json nga cmimradar.netlify.app).

Veçoritë:
  🔍 kërkim çmimi me tekst të lirë
  ⌨️ menu me butona (reply keyboard)
  🔥 /oferta — oferta e ditës
  📰 /digest — abonim në top-5 uljet e ditës
  🔔 alarme uljeje via deep-link (tg_/em_) + /alerts me butona fshirjeje
  🎯 alarme push për produkte (via Netlify Blobs push-subscribe)
  📸 oferta nga komuniteti (ruhen në bot-state function)
  📊 përmbledhje admin kur del raport i ri

State-i mbahet në Netlify Blobs via function-i `bot-state`
(përndryshe disku efemer i Render-it do ta humbiste).

Env vars (Render dashboard):
  TG_BOT_TOKEN, VAPID_PRIVATE, BOT_KEY, TG_ADMIN_CHAT, SITE_URL
"""
import base64
import html as htmlmod
import json
import os
import random
import re
import time
import traceback
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- config
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE", "").strip()
BOT_KEY = os.environ.get("BOT_KEY", "").strip()
ADMIN_CHAT = os.environ.get("TG_ADMIN_CHAT", "").strip()
SITE_URL = os.environ.get("SITE_URL", "https://cmimradar.netlify.app").rstrip("/")
STATE_URL = SITE_URL + "/.netlify/functions/bot-state"
PUSH_URL = SITE_URL + "/.netlify/functions/push-subscribe"
PUSH_GAP_THRESHOLD = 20.0

DATA_TTL = 15 * 60        # rifreskim të dhënash nga website
PRICE_CHECK_EVERY = 20 * 60
HEARTBEAT_EVERY = 5 * 60
REPORT_WATCH_EVERY = 10 * 60

for _v in ("TG_BOT_TOKEN", "BOT_KEY"):
    if not globals()[_v]:
        raise SystemExit("Mungon env var: " + _v)

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

# ---------------------------------------------------------------- state (memory + flush)
STATE = {}
STATE_DIRTY = False


def default_state():
    return {"offset": 0, "subs": [], "pending": {}, "digest_subs": [],
            "awaiting_search": [], "last_push_date": "", "last_digest_date": "",
            "last_report_date": "", "store_fails": {}}


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
DATA = {"products": None, "report": None, "ts": 0, "bykey": {}}


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
        DATA["bykey"] = {g["key"]: g for g in prods.get("groups", [])}
        DATA["ts"] = time.time()
        print("Të dhënat u rifreskuan (%d grupe)." % len(DATA["bykey"]), flush=True)
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
    "👋 <b>Përshëndetje! Unë jam boti i ÇmimRadar-it</b> 🤖\n\n"
    "🔍 Më shkruaj emrin e produktit dhe të them <b>çmimin më të lirë</b> në Shqipëri.\n"
    "🔔 Vendos <b>alarme uljeje</b> nga faqja — të njoftoj këtu sapo çmimi të bjerë.\n"
    "📸 Dërgomë <b>foto-ofertë</b> me <code>Produkti | Dyqani | Çmimi</code> — e publikojmë pas verifikimit.\n\n"
    "Përdor butonat më poshtë ose komandat:\n"
    "🔥 /oferta · 📰 /digest · 📋 /alerts · ❓ /help\n\n"
    "🌐 " + SITE_URL
)

BOT_HELP = (
    "❓ <b>Ndihma — ÇmimRadar Bot</b>\n\n"
    "🔍 <b>Kërko:</b> shkruaj p.sh. <i>iphone 17</i> ose shtyp 🔍 Kërko.\n"
    "🔔 <b>Alarme:</b> nga faqja hap produktin → 🔔 <b>Alarm uljeje</b> → tabin <b>Telegram</b> → "
    "<b>Vazhdo</b> → <b>START</b> këtu. Fshihen me /alerts.\n"
    "🔥 <b>/oferta</b> — oferta e ditës.\n"
    "🏆 <b>/top</b> — top 10 uljet e ditës.\n"
    "📰 <b>/digest</b> — abonohu në top-5 uljet e ditës (çdo mbrëmje).\n"
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
NORM_RE = re.compile(r"[^a-z0-9çë\s]")


def norm(s):
    return NORM_RE.sub(" ", (s or "").lower()).split()


def fmt_price(p):
    try:
        return f"{float(p):,.0f}".replace(",", ".")
    except Exception:
        return "—"


def product_link(key):
    return SITE_URL + "/product.html?key=" + key


def user_subs(chat_id):
    return [s for s in STATE.get("subs", [])
            if s.get("channel") == "telegram" and str(s.get("chat_id")) == str(chat_id)]

# ---------------------------------------------------------------- kërkimi
def search_products(query, limit=5):
    words = [w for w in norm(query) if len(w) > 1]
    if not words:
        return []
    scored = []
    for g in DATA["bykey"].values():
        tw = [t for t in norm(g.get("title", "")) if len(t) > 1]
        if not tw:
            continue
        hit = 0
        for w in words:
            for t in tw:
                if w == t or (len(w) >= 4 and w in t) or (len(t) >= 4 and t in w):
                    hit += 1
                    break
        if hit == 0:
            continue
        # pikë: sa fjalë u gjetën + bonus për titull të shkurtër (më i saktë)
        score = hit * 10 - len(tw) * 0.05
        best = g.get("best") or {}
        if best.get("price"):
            score += 2
        scored.append((score, g))
    scored.sort(key=lambda x: -x[0])
    return [g for _, g in scored[:limit]]


def format_product(g, key):
    best = g.get("best") or {}
    price = best.get("price")
    store = best.get("store_name", "")
    offers = g.get("offers", [])
    lines = ["🔎 <b>%s</b>" % (g.get("title", "")[:90])]
    if price:
        lines.append("💰 <b>%s L</b> te <b>%s</b>" % (fmt_price(price), store))
        if len(offers) > 1:
            mx = max(o.get("price", 0) for o in offers)
            if mx and mx > price:
                lines.append("📊 %d dyqane · diferenca %s%%" %
                             (len(offers), round((mx - price) / mx * 100, 1)))
        sig = g.get("buy_signal")
        if sig == "rekord":
            lines.append("🔥 <b>BLEJ TANI</b> — çmimi më i ulët ndonjëherë!")
        elif sig == "mire":
            lines.append("✅ <b>Çmim i mirë</b> — ndër më të ulëtit historik.")
        elif sig == "shtrenjte":
            lines.append("⏳ <b>Prit</b> — çmimi është i lartë tani.")
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
        btns = [[{"text": ("✅ " if i == 0 else "") + r["title"][:40],
                  "callback_data": "prod:%s" % r["key"]}] for i, r in enumerate(res[1:4])]
        tg_send(chat_id, "🔎 <b>Rezultate të tjera:</b>", markup={"inline_keyboard": btns})

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
    name = dod.get("name", "")[:90]
    price = dod.get("price")
    store = dod.get("store_name", "")
    old = dod.get("old_price")
    key = dod.get("key", "")
    lines = ["🔥 <b>OFERTA E DITËS</b>\n", "🏷️ <b>%s</b>" % name]
    if price:
        lines.append("💰 <b>%s L</b> te <b>%s</b>" % (fmt_price(price), store))
    if old and price and old > price:
        lines.append("📉 Ishte %s L (−%s%%)" %
                     (fmt_price(old), round((old - price) / old * 100, 1)))
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


def cmd_top(chat_id):
    if not refresh_data():
        tg_send(chat_id, "⚠️ S'munda të lexoj të dhënat. Provo pas pak.")
        return
    rep = DATA["report"] or {}
    drops = rep.get("drops") or []
    if not drops:
        tg_send(chat_id, "📊 S'ka ulje sot. Kthehu nesër!")
        return
    lines = ["🏆 <b>TOP 10 ULJET E DITËS</b>\n"]
    for i, d in enumerate(drops[:10], 1):
        title = (d.get("title") or "")[:55]
        price = d.get("price")
        old = d.get("old_price")
        store = d.get("store_name", "")
        pct = ""
        if old and price and old > price:
            pct = " (−%s%%)" % round((old - price) / old * 100, 1)
        lines.append("%d. <b>%s</b>\n   💰 %s L%s te %s" % (
            i, title, fmt_price(price), pct, store))
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
        lines.append("• <b>%s</b>\n  synim ≤ <b>%s L</b>" % (s["name"][:60], fmt_price(s["target"])))
        btns.append([{"text": "❌ %s" % s["name"][:28],
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
        key = data.split(":", 1)[1]
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
                   "target": target, "name": g["title"][:80]}
        exists = any(s.get("channel") == "telegram"
                     and str(s.get("chat_id")) == str(chat_id)
                     and s.get("key") == key and s.get("target") == target
                     for s in subs)
        if not exists:
            subs.append(new_sub)
            mark_dirty()
            record_op({"sub_add": new_sub})
        tg_send(chat_id,
                "✅ <b>Alarmi u aktivizua!</b>\n\n<b>%s</b>\n"
                "Do të njoftohesh këtu kur çmimi të bjerë në <b>%s L</b> ose më pak.\n\n" % (
                    g["title"][:80], fmt_price(target)) +
                ("Ky alarm ekzistonte tashmë — nuk u dyfishua." if exists else
                 "Çmimi tani: %s L te %s." % (
                     fmt_price((g.get("best") or {}).get("price")),
                     (g.get("best") or {}).get("store_name", ""))),
                markup=MAIN_KB)
    else:
        pend = {"key": key, "target": target, "name": g["title"][:80]}
        STATE.setdefault("pending", {})[str(chat_id)] = pend
        mark_dirty()
        record_op({"pending_put": {"chat_id": str(chat_id), "data": pend}})
        tg_send(chat_id,
                "📧 <b>%s</b>\n\nShkruaj <b>email-in tënd</b> si mesazh këtu që ta "
                "konfirmojmë alarmin (synimi: %s L)." % (g["title"][:80], fmt_price(target)))


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
    fid = photos[-1]["file_id"]
    fpath = tg_get_file(fid)
    data = tg_download_file(fpath) if fpath else None
    pid = "c%d_%04d" % (int(time.time()), random.randint(0, 9999))
    ok = bool(bs_photo_put({
        "id": pid, "chat_id": chat_id, "product": product, "store": store,
        "price": price, "caption": cap, "date": time.strftime("%Y-%m-%d %H:%M"),
        "data_b64": base64.b64encode(data).decode() if data else ""}))
    tg_send(chat_id,
            ("📸 <b>Oferta u mor!</b>\n\n" if ok else "📸 <b>Oferta u mor, por fotoja s'u ruajt!</b>\n\n") +
            "<b>%s</b>%s%s\n\n"
            "Do ta verifikojmë dhe publikojmë te “Oferta nga komuniteti” "
            "sa më shpejt. Faleminderit! 🤝" %
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
        handle_search(chat_id, txt)
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

    # tekst i lirë → kërkim
    handle_search(chat_id, txt)


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

# ---------------------------------------------------------------- njoftimet (alarme çmimesh)
def send_email(to, subject, body):
    import subprocess
    r = subprocess.run(["hatch_gws_cli", "gmail", "+send", "--to", to,
                        "--subject", subject, "--body", body],
                       capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0
    if not ok:
        print("  !! email dështoi për %s: %s" % (to, (r.stderr or r.stdout)[:150]), flush=True)
    return ok


def check_price_alerts():
    if not refresh_data():
        return 0
    sent, keep = 0, []
    for s in STATE.get("subs", []):
        g = DATA["bykey"].get(s.get("key"))
        if not g or not g.get("best"):
            keep.append(s)
            continue
        price = g["best"]["price"]
        if price and price <= s["target"]:
            store = g["best"].get("store_name", "")
            url = g["best"].get("url", "")
            if s["channel"] == "telegram":
                ok = tg_send(s["chat_id"],
                             "🎯 <b>ÇmimRadar — alarmi u aktivizua!</b>\n\n"
                             "<b>%s</b>\nZbriti në <b>%s L</b> te <b>%s</b> "
                             "(synimi: %s L).\n%s" %
                             (s["name"], fmt_price(price), store, fmt_price(s["target"]), url))
            else:
                ok = send_email(s["email"],
                                "🎯 ÇmimRadar: %s zbriti në %s L" % (s["name"][:50], fmt_price(price)),
                                "%s\nZbriti në %s L te %s (synimi: %s L).\n%s" %
                                (s["name"], fmt_price(price), store, fmt_price(s["target"]), url))
            if ok:
                sent += 1
                time.sleep(1)
            else:
                keep.append(s)
        else:
            keep.append(s)
    if len(keep) != len(STATE.get("subs", [])):
        STATE["subs"] = keep
        mark_dirty()
    return sent

# ---------------------------------------------------------------- push
def push_blobs_get():
    try:
        req = urllib.request.Request(PUSH_URL, headers={"x-push-key": BOT_KEY}, method="GET")
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if isinstance(d, dict):
            return d.get("subs") or [], d.get("alerts") or []
    except Exception as e:
        print("  !! push_blobs_get: %s" % str(e)[:120], flush=True)
    return [], []


def push_blobs_delete(endpoint=None, alert=None):
    try:
        body = {"push_alert_del": alert} if alert else {"endpoint": endpoint}
        data = json.dumps(body).encode()
        req = urllib.request.Request(PUSH_URL, data=data,
                                     headers={"Content-Type": "application/json",
                                              "x-push-key": BOT_KEY}, method="DELETE")
        with urllib.request.urlopen(req, timeout=30):
            pass
        return True
    except Exception as e:
        print("  !! push_blobs_delete: %s" % str(e)[:120], flush=True)
        return False


def webpush_send(sub, payload):
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("  !! pywebpush mungon", flush=True)
        return False, False
    if not VAPID_PRIVATE:
        print("  !! VAPID_PRIVATE mungon", flush=True)
        return False, False
    try:
        webpush(sub, payload, vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": "mailto:admin@cmimradar.netlify.app"})
        return True, False
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        print("  !! push %s: %s" % (code, str(e)[:100]), flush=True)
        return False, code in (404, 410)


def check_push_alerts():
    subs, alerts = push_blobs_get()
    if not alerts or not refresh_data():
        return 0
    sent = 0
    for a in alerts:
        g = DATA["bykey"].get(a.get("key"))
        ep = a.get("endpoint", "")
        if not g or not g.get("best"):
            continue
        price = g["best"]["price"]
        target = a.get("target") or 0
        if price and price <= target:
            name = (a.get("name") or g.get("title", ""))[:60]
            payload = json.dumps({
                "title": "🎯 ÇmimRadar: %s zbriti!" % name,
                "body": "%s L te %s (synimi: %s L)" %
                        (fmt_price(price), g["best"].get("store_name", ""), fmt_price(target)),
                "url": product_link(a["key"]),
            }, ensure_ascii=False)
            ok, dead = webpush_send({"endpoint": ep, "keys": a.get("keys") or {}}, payload)
            if ok:
                sent += 1
            if dead:
                push_blobs_delete(endpoint=ep)
            if ok or dead:
                push_blobs_delete(alert={"endpoint": ep, "key": a["key"]})
            time.sleep(0.3)
    return sent


def send_push_digest():
    from datetime import date
    today = date.today().isoformat()
    if STATE.get("last_push_date") == today:
        return 0
    subs, _ = push_blobs_get()
    if not subs or not refresh_data():
        return 0
    rep = DATA["report"] or {}
    gaps = rep.get("biggest_gaps") or []
    if not gaps:
        return 0
    try:
        pct = float(gaps[0].get("gap_pct") or 0)
    except Exception:
        return 0
    if pct < PUSH_GAP_THRESHOLD:
        return 0
    payload = json.dumps({
        "title": "🔥 ÇmimRadar: %.1f%% diferencë!" % pct,
        "body": "%s — krahaso çmimet para se të blesh." % (gaps[0].get("name") or "")[:60],
        "url": SITE_URL + "/#raporti",
    }, ensure_ascii=False)
    sent = 0
    for s in subs:
        ok, dead = webpush_send(s, payload)
        if ok:
            sent += 1
        elif dead:
            push_blobs_delete(endpoint=s.get("endpoint", ""))
        time.sleep(0.3)
    if sent:
        STATE["last_push_date"] = today
        mark_dirty()
    return sent

# ---------------------------------------------------------------- digest ditor
def send_daily_digest():
    from datetime import date
    today = date.today().isoformat()
    if STATE.get("last_digest_date") == today:
        return 0
    subs = [str(x) for x in STATE.get("digest_subs", [])]
    if not subs or not refresh_data():
        return 0
    rep = DATA["report"] or {}
    drops = rep.get("drops") or []
    if not drops:
        return 0
    lines = ["📰 <b>ÇmimRadar — top uljet e sotme</b>\n"]
    for d in drops[:5]:
        lines.append("• <b>%s</b>\n  %s L <i>(ishte %s)</i> −%s%%" %
                     ((d.get("name") or "")[:55], fmt_price(d.get("price")),
                      fmt_price(d.get("old_price")), round(d.get("drop_pct", 0), 1)))
    lines.append("\n🌐 " + SITE_URL)
    msg = "\n".join(lines)
    sent = 0
    for chat_id in subs:
        if tg_send(chat_id, msg):
            sent += 1
        time.sleep(0.5)
    STATE["last_digest_date"] = today
    mark_dirty()
    return sent

# ---------------------------------------------------------------- admin summary
def admin_summary():
    if not ADMIN_CHAT:
        return False
    rep = DATA["report"] or {}
    stats = rep.get("stats", {}) or {}
    stores = rep.get("stores", {}) or {}
    dead = sorted([s.get("name", k) for k, s in stores.items() if not s.get("products")])
    fails = STATE.get("store_fails", {})
    for k, s in stores.items():
        nm = s.get("name", k)
        fails[nm] = fails.get(nm, 0) + 1 if not s.get("products") else 0
    STATE["store_fails"] = {k: v for k, v in fails.items() if v > 0}
    mark_dirty()
    chronic = sorted([k for k, v in fails.items() if v >= 3])
    lines = [
        "📊 <b>ÇmimRadar — skanimi %s</b>" % rep.get("date", "?"), "",
        "🛍️ %s produkte · %s grupe · %s dyqane" %
        (fmt_price(stats.get("products", 0)), fmt_price(stats.get("groups", 0)),
         stats.get("stores", len(stores))),
        "📉 %d ulje · 📈 %d rritje" % (stats.get("drops", 0), stats.get("rises", 0)), "",
        "🔔 Alarme TG/email aktive: %d" % len(STATE.get("subs", [])),
        "📰 Digest abonentë: %d" % len(STATE.get("digest_subs", [])),
    ]
    if dead:
        lines += ["", "⚠️ Dyqane me 0 produkte: " + ", ".join(dead[:8])]
    if chronic:
        lines += ["", "🚨 Bien 3+ ditë: " + ", ".join(chronic)]
    if not dead and not chronic:
        lines += ["", "✅ Të gjitha dyqanet skanuan normalisht."]
    return tg_send(ADMIN_CHAT, "\n".join(lines))

# ---------------------------------------------------------------- jobs
JOB_TIMERS = {}


def every(name, seconds):
    now = time.time()
    if now - JOB_TIMERS.get(name, 0) >= seconds:
        JOB_TIMERS[name] = now
        return True
    return False


def run_jobs():
    try:
        if every("heartbeat", HEARTBEAT_EVERY):
            bs_request("POST", {"heartbeat": int(time.time())})
        if every("data", DATA_TTL):
            refresh_data(force=True)
        # raport i ri? → admin summary (+ digest pas skanimit të mbrëmjes)
        if every("report_watch", REPORT_WATCH_EVERY) and refresh_data():
            rdate = (DATA["report"] or {}).get("date", "")
            if rdate and rdate != STATE.get("last_report_date"):
                STATE["last_report_date"] = rdate
                mark_dirty()
                print("Raport i ri: %s → admin summary" % rdate, flush=True)
                admin_summary()
                send_daily_digest()
        if every("prices", PRICE_CHECK_EVERY):
            n = check_price_alerts()
            p = check_push_alerts()
            d = send_push_digest()
            if n or p or d:
                print("Alarme: tg/email=%d push=%d digest=%d" % (n, p, d), flush=True)
        save_state()
    except Exception:
        print("  !! gabim në jobs:\n%s" % traceback.format_exc()[:600], flush=True)

# ---------------------------------------------------------------- main loop
def run_polling():
    """Long polling — për testim lokal."""
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
        run_jobs()


# ---------------------------------------------------------------- ops (intent-tracking)
# Çdo ndryshim i state-it regjistrohet si operacion atomik; në webhook mode
# dërgohen VETËM ops-et (kurrë mbishkrim i plotë) → pa gara me Blobs.
PENDING_OPS = []


def record_op(op):
    PENDING_OPS.append(op)


def flush_ops():
    ok = True
    for op in PENDING_OPS:
        if bs_request("POST", op) is None:
            ok = False
    PENDING_OPS.clear()
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
            except Exception:
                print("  !! gabim në webhook:\n%s" % traceback.format_exc()[:400], flush=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    # regjistro webhook-un te Telegram (URL nga Render)
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if base:
        wh_url = base + secret_path
        r = tg("setWebhook", {"url": wh_url, "max_connections": 40,
                              "secret_token": secret_token,
                              "allowed_updates": ["message", "callback_query"]})
        print("setWebhook: %s -> %s" % (wh_url, bool(r and r.get("ok"))), flush=True)
    else:
        print("RENDER_EXTERNAL_URL mungon — webhook s'u regjistrua.", flush=True)

    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Webhook server në portin %d." % port, flush=True)
    # Në webhook mode NUK ka jobs loop: shërbimi fle kur s'ka mesazhe,
    # kontrollet periodike (alarme, digest, përmbledhje admin) i bën cron-i i VM-së.
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    print("ÇmimRadar Bot worker u nis.", flush=True)
    load_state()
    refresh_data(force=True)
    me = tg("getMe", {})
    print("Bot: %s" % ((me.get("result") or {}).get("username") if me else "?"), flush=True)
    if os.environ.get("PORT"):
        # webhook mode: pa mesazh "u ndez" (cold start-et janë të shpeshta)
        run_webhook()
    else:
        if ADMIN_CHAT:
            tg_send(ADMIN_CHAT, "🤖 <b>ÇmimRadar Bot u ndez.</b>\nPërgjigjet në sekonda. ✅")
        run_polling()


if __name__ == "__main__":
    main()
