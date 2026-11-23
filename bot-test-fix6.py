"""Regression for bot §6 fix (FINAL FIX PASS) + site-parity rules.
Loads radar_logic from the canonical repo, downloads FRESH live
products.json, runs the key queries and asserts the expected lead offer.
Usage: python3 bot-test-fix6.py [products.json path] [radar_logic.py path]
Defaults: fresh download from cmimradar.netlify.app; ~/workspace/cmimradar-bot/radar_logic.py
Exit 0 = all pass, 1 = any fail.
"""
import importlib.util, json, os, subprocess, sys

PROD_URL = "https://cmimradar.netlify.app/data/products.json"
REPO = os.path.expanduser("~/workspace/cmimradar-bot/radar_logic.py")

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

prod_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bot-test-products.json"
if len(sys.argv) <= 1:
    subprocess.run(["curl", "-sS", "--max-time", "60", PROD_URL,
                    "-o", prod_path], check=True)
rl = load("rl_fixed", sys.argv[2] if len(sys.argv) > 2 else REPO)
prods = json.load(open(prod_path, encoding="utf-8"))
merged, _ = rl.merge_groups(prods["groups"])
print(f"groups: {len(merged)} date: {prods.get('date')}")

def lead(query):
    r = rl.answer_ai(merged, query)
    picks = r.get("picks") or []
    if not picks:
        return None, None, r
    o = picks[0]["offer"]
    return o.get("store"), o.get("price"), r

CASES = [
    # (query, expected_store, expected_price, note)
    ("iphone 17 më i liri në stok", "tigo", 74900, "in-stock lead"),
    ("iphone 17 më i liri", "tigo", 74900, "unverified Celular 71146 must NOT win"),
    # US-carrier fix (Item 1/5, 2026-11-23): the old expectation
    # ("3vfejzo", 112900) WAS the AT&T offer — the defect. The lead is
    # now the cheapest plain EU in-stock offer, shpresa 114990.
    ("iphone 17 pro max", "shpresa", 114990, "Pro Max lead (AT&T 112900 + USA e-SIM 112000 excluded)"),
    ("iphone 17 pro max më i liri në stok", "shpresa", 114990, "Pro Max in-stock"),
    ("iphone 17 pro max 512gb", "molla", 170335, "Pro Max 512GB (AT&T 117900 excluded)"),
    ("iphone 17 pro max 1tb", "molla", 193779, "Pro Max 1TB (AT&T 147900 excluded)"),
    # 17e: lead STAYS tigo 52500 E-SIM — legitimate fallback, its only
    # plain offer (shpresa 57990) is OUT OF STOCK. Asserted as eSIM
    # below, not as a different lead.
    ("iphone 17e", "tigo", 52500, "17e eSIM fallback (only plain offer is OOS)"),
    # Pro: merged duplicate group f0dfee4e (americanpc 96900, real new offer).
    # Cluster merge landed 2026-11-23 (data side): the merged group best
    # is americanpc 96900 — this expectation now tests the merged group.
    ("iphone 17 pro", "americanpc", 96900, "merged duplicate group"),
    # Air base group merged 2026-11-23 ("Air" 90896462 + "17 Air" 2cfc9f6a):
    # the merged base best is boostmobile 76000 (was smartphoneshop 77900
    # pre-merge). Correct new lead, not a regression.
    ("iphone 17 air", "boostmobile", 76000, "Air lead (merged base group)"),
    ("tv 55 deri 60000", "globe", 23900, "TV budget"),
    ("kufje bluetooth deri 3000", "gotech", 490, "headphones budget"),
    ("iphone 17 nën 80000", "tigo", 74900, "budget cap"),
    ("sa kushton iphone 17?", "tigo", 74900, "price question"),
]
fails = 0
for q, exp_store, exp_price, note in CASES:
    store, price, _ = lead(q)
    ok = (store == exp_store and price == exp_price)
    print(("PASS " if ok else "FAIL ") + f"{q!r}: {store} {price} (pritet {exp_store} {exp_price}) — {note}")
    fails += 0 if ok else 1

# s26 ultra: preorder — KNOWN OPEN item (bot answers Celular preorder,
# site says none). Lead must stay celular 73851 until preorder handling
# is decided separately.
store, price, _ = lead("samsung s26 ultra")
ok = (store, price) == ("celular", 73851)
print(("PASS " if ok else "FAIL ") + f"'samsung s26 ultra': lead={store} {price} (pritet celular 73851 — preorder, item i hapur)")
fails += 0 if ok else 1

# 17e: the lead must be the eSIM offer AND flagged as such (fallback rule)
r17 = rl.answer_ai(merged, "iphone 17e")
p17 = (r17.get("picks") or [{}])[0].get("offer") or {}
ok = (p17.get("store"), p17.get("price")) == ("tigo", 52500) and bool(p17.get("is_esim"))
print(("PASS " if ok else "FAIL ") + f"'iphone 17e' lead flagged eSIM: {p17.get('store')} {p17.get('price')} is_esim={p17.get('is_esim')} (pritet tigo 52500, is_esim=True — fallback)")
fails += 0 if ok else 1

# Direct comparable_offers() assertions on the Pro Max group.
# Group key changed in the 2026-11-23 rebuild (merge of the Globe
# duplicates re-keyed it): new 51e459b347fb2104, old 60acb9d66949bd76
# (still resolvable via products.json aliases). Try the new key first.
pm_group = next((g for g in merged if g.get("key") in
                 ("51e459b347fb2104", "60acb9d66949bd76")), None)
if pm_group is None:
    pm_group = next((g for g in prods["groups"] if g.get("key") in
                     ("51e459b347fb2104", "60acb9d66949bd76")), None)
if pm_group is None:
    print("FAIL comparable_offers: Pro Max group (51e459b347fb2104 / 60acb9d66949bd76) not found")
    fails += 1
else:
    comp = rl.comparable_offers(pm_group)
    ok = not any(o.get("store") == "3vfejzo" and o.get("price") == 112900 for o in comp)
    print(("PASS " if ok else "FAIL ") + "comparable_offers Pro Max: AT&T 3vfejzo 112900 absent")
    fails += 0 if ok else 1
    ok = not any(o.get("store") == "americanpc" and o.get("price") == 112000 for o in comp)
    print(("PASS " if ok else "FAIL ") + "comparable_offers Pro Max: americanpc USA e-SIM 112000 absent")
    fails += 0 if ok else 1
    ok = any(o.get("store") == "shpresa" and o.get("price") == 114990 for o in comp)
    print(("PASS " if ok else "FAIL ") + "comparable_offers Pro Max: shpresa 114990 present")
    fails += 0 if ok else 1
    cv = rl.cheapest_valid(pm_group)
    ok = cv is not None and (cv.get("store"), cv.get("price")) == ("shpresa", 114990)
    print(("PASS " if ok else "FAIL ") + f"cheapest_valid Pro Max: {cv.get('store') if cv else None} {cv.get('price') if cv else None} (pritet shpresa 114990)")
    fails += 0 if ok else 1
    ok = all(not rl.is_usa(o) for o in comp)
    print(("PASS " if ok else "FAIL ") + "comparable_offers Pro Max: zero USA offers remain")
    fails += 0 if ok else 1

# frigorifer: no fridge under 1000 L — answer comes via FALLBACK, not picks
r = rl.answer_ai(merged, "frigorifer deri 1000 L")
fb = r.get("fallback") or {}
title = (fb.get("group") or {}).get("title", "")
ok = any(w in title.lower() for w in ("midea", "frigorifer", "minibar", "fridge"))
print(("PASS " if ok else "FAIL ") + f"'frigorifer deri 1000 L' (fallback): {title[:60]!r} (pritet frigorifer real)")
fails += 0 if ok else 1

# General queries: must not crash
GENERAL = ["iphone më i liri", "ps5", "ps5 më i liri", "laptop më i liri",
           "tv 55", "kufje më i liri", "samsung galaxy a55", "xiaomi redmi note 13",
           "iphone 16 pro max", "macbook air m4", "galaxy z fold 7"]
for q in GENERAL:
    try:
        r = rl.answer_ai(merged, q)
        print(f"OK   {q!r}: {len(r.get('picks') or [])} picks")
    except Exception as e:
        print(f"FAIL {q!r}: {type(e).__name__}: {e}")
        fails += 1

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
