# RADAR Bot — Render Background Worker

Boti Telegram i RADAR-it (`@cmimradarbot`): përgjigje në sekonda
(long polling 24/7 në testim lokal, webhook në Render).
Lexon të dhënat live nga https://cmimradar.netlify.app/data/*.json
(`products.json`, `report.json`, `history.json`).
State-i mbahet në Netlify Blobs via function-i `bot-state`.

## File-t

| File | Çfarë është |
|---|---|
| `bot_worker.py` | Boti: komanda, kërkim, "Pyet Radarin", deep-linke, foto komuniteti, webhook + heartbeat. |
| `radar_logic.py` | **Rregullat kanonike** (stdlib vetëm): kërkimi, çmimi më i lirë me dy nivele stoku, verdikti, formatimi, linket `/p/<slug>-<id>`, `week_drops()`. Përdoret edhe nga pilot-i (sys.path). |
| `render.yaml`, `requirements.txt` | Si në repo (të pandryshuara). |

## Rregullat e sinkronizimit (përmbledhje)

- **Çmimi më i lirë (dy nivele):** ofertat e dyshimta (>3× ose <⅓ e
  medianës së grupit), "Vitrinë" dhe "I përdorur" përjashtohen kudo;
  `in_stock: false` nuk është kurrë më i liri. "Më i liri" mund të ketë
  `in_stock: null` → etiketa **"stoku i pakonfirmuar"**; veçmas shfaqet
  **"Më i liri në stok"** kur ndryshon.
- **Verdikti** vjen vetëm nga historia (`BLI TANI` / `PRIT` / `SHMANG`);
  pa histori (ose < 2 pika) → **"Pa verdikt · pa histori"**.
- **"Ishte … (−…%)"** merret VETËM nga `report.json` drops (`prev`/`pct`)
  — kurrë nga `old_price` i dyqanit.
- **Kërkimi** punon mbi grupet e **bashkuara** (19.497 → 19.370):
  sinonime (`ajfon` → `iphone`), fraza (`fshese me korrent`),
  numra të detyrueshëm (`ps5` ≠ `ps4`), përjashtim aksesorësh.
- **"Pyet Radarin":** teksti me buxhet ose me model pyetjeje
  (`më i liri`, `ku është më lirë`, `cheapest`, `sa kushton`, `nën`,
  `deri`, `për student`, …) shkon te `answer_ai()` — buxheti është
  **kufi i fortë**, kthehen vetëm produkte reale (maks. 3), ose
  alternativa më e lirë reale mbi buxhet, ose teksti i ndershëm
  "Nuk gjeta…". Përgjigjja vjen në gjuhën e pyetjes (sq/en).
- **Fushat e skanerit** (`group["cheapest"]` / `["best_in_stock"]` /
  `["verdict"]`, nga `build.py` / `buy_signal.py` pas skanimit tjetër)
  parapëlqehen kur ekzistojnë; përndryshe boti i llogarit pastër me
  `radar_logic`. Për grupet e **bashkuara** fushat e skanerit injorohen
  (i përkasin vetëm grupit të parë burim) dhe përdoret gjithmonë
  llogaritja e pastër. `group["best"]` i vjetër nuk përdoret kurrë si
  çmimi më i lirë (mund të jetë ofertë pa stok).
- **Linket:** produkti `/p/<slug>-<id>` (p.sh.
  `/p/samsung-galaxy-z-fold7-388acc4cf2ae3b4d`), dyqani
  `Bli te <dyqani> ↗` me URL-në e ofertës. Çmimet me pikë dhjetore
  (`129.990 L`), datat shqip (`19 nëntor 2026`).
- **Branding:** `RADAR` kudo në tekstet e dukshme. Handle-i mbetet
  `@cmimradarbot`, faqja `https://cmimradar.netlify.app`.
- **Alarmet / digest / admin summary** dërgohen VETËM nga cron-i
  (`check_remote_alerts.py` pas çdo skanimi). Kopjet e fjetura në bot
  u hoqën më 2026-11-19; këtu mbetet vetëm **heartbeat-i**
  (`write_heartbeat()` çdo 5 min + në çdo update), të cilin e lexon
  health_check C-BOT-2.
- **Foto komuniteti:** oferta e re dërgohet me
  `status: "në pritje"` dhe përdoruesi e sheh etiketën
  `Statusi: në pritje` në konfirmim.

## Testimi lokal (offline)

```bash
# 1. Sintaksa
python3 -m py_compile bot_worker.py radar_logic.py

# 2. Verifikimi i plotë offline (pa rrjet, pa token, pa mesazhe live)
python3 ~/workspace/radar-verify/test_bot_worker.py
# -> 111 checks; pritet 107 PASS + 4 FAIL të dokumentuara
#    (2 ranking "iphone 17" / "ajfon" + alias REDMI + Pro Max BLI TANI -> FLAT — shih shënimin më poshtë)
```

Testi vendos `TG_BOT_TOKEN` / `BOT_KEY` fallco, i lexon të dhënat reale
lokale (`~/workspace/cmimradar-site/data/*.json`) dhe i kap mesazhet
dalëse në vend që t'i dërgojë.

**4 FAIL të njohura (të qëllimshme, jo bug-e të kodit):** renditja
kanonike (përputhje kategorie → fillim titulli → çmimi më i lirë) e
kërkimit `iphone 17` jep `iPhone 17 — 256 GB esim` 66.721 L dhe e
`ajfon` jep `iPhone X` 7.500 L — jo `iPhone 17 Pro Max` 82.900 L siç
pritej në brief. Implementimi i referencës mbi të njëjtat të dhëna
jep ekzaktësisht të njëjtin rezultat (të 11 pyetjet AI përputhen 11/11
me implementimin e referencës). Pro Max-i nuk është më i liri; ta
nxjerrësh të parin do të kërkonte një rregull shtesë (p.sh. barazim
→ më shumë dyqane së pari) që spec-i nuk e përmban. Vendimi i mbetet
koordinatorit — shih `~/workspace/radar-verify/bot-sync-notes-B.md`.
Dy FAIL-et e tjera: alias REDMI (artefakt i të dhënave të 19 nëntorit)
dhe Pro Max BLI TANI → FLAT (histori e sheshtë = "Çmim i qëndrueshëm",
sjellja e synuar e fix-it FLAT).

## Testimi live (i bllokuar)

Nuk ekziston bot testues. Verifikimi live kërkon që përdoruesi të
krijojë një bot testues përmes BotFather — shënuar në checklist-in e
deploy-it (bot-sync). Mos e testo kurrë me botin e prodhimit.

## Env vars (Render dashboard)

`TG_BOT_TOKEN`, `BOT_KEY`, `TG_ADMIN_CHAT`, `SITE_URL`
(opsionale; default `https://cmimradar.netlify.app`).

## Webhook-u i prodhimit — KUJDES

Statusi i webhook-ut të prodhimit (`@cmimradarbot`) kontrollohet
**VETËM** me `check_webhook_health.py`
(`~/workspace/cmimradar-pilot/check_webhook_health.py`), i cili e lexon
token-in e prodhimit nga goal-i i ÇmimRadar-it. **Kurrë përmes
connector-it `custom.telegram-bot-api`** — ai mban token-in e botit të
testit (`rada1r_bot`) dhe `getWebhookInfo` përmes tij kthen `url: ""`
(gjendja e testit, jo e prodhimit).
