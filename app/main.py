"""Upwork Autopilot: one always-on service.

Loops: poll Upwork -> filter -> score -> draft -> Telegram; Telegram long-poll for your taps;
hourly maintenance (24h purge, 8am digest, outcome follow-ups). Plus a small password-protected dashboard.
"""
import asyncio
import datetime as dt
import html
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import ai, db, filters, telegram, upwork
from .config import (DASHBOARD_PASSWORD, INGEST_WEBHOOK_SECRET, KNOWLEDGE_DIR, RULES_PATH, TELEGRAM_CHAT_ID,
                     load_rules, save_rules)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")
DRAFT_SLOTS = asyncio.Semaphore(4)


# ---------------------------------------------------------------- pipeline

def drafts_today() -> int:
    midnight = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return db.counts_since(midnight).get("drafted", 0)


async def process(job: dict) -> None:
    """One job through the three gates. Safe to run concurrently."""
    rules = load_rules()
    db.save_job(job)
    reason = filters.check(job, rules)
    if reason:
        db.log(job["id"], "filtered", {"reason": reason})
        return
    if not ai.client:
        await telegram.send(telegram.job_card(job, None, None))
        return
    try:
        async with DRAFT_SLOTS:
            scored = await ai.score(job)
            db.log(job["id"], "scored", scored)
            threshold = rules.get("score_threshold", 70)
            if scored["score"] < threshold:
                return
            if drafts_today() >= rules.get("max_drafts_per_day", 40):
                await telegram.send(telegram.job_card(job, scored, None) + "\n\n<i>Daily draft cap reached; not drafted.</i>")
                return
            drafted = await ai.draft(job, scored)
            db.log(job["id"], "drafted", drafted)
        await telegram.send(telegram.job_card(job, scored, drafted), telegram.decision_buttons(job["id"]))
    except Exception as e:  # noqa: BLE001 — one bad job must never stop the loop
        log.exception("process failed for %s", job["id"])
        db.log(job["id"], "error", {"error": str(e)[:500]})


async def ingest(jobs: list[dict]) -> int:
    fresh = [j for j in jobs if db.mark_seen(j["id"], j["source"])]
    for j in fresh:
        asyncio.create_task(process(j))
    return len(fresh)


# ---------------------------------------------------------------- loops

async def poll_loop() -> None:
    failures = 0
    while True:
        rules = load_rules()
        if upwork.connected() and not db.kv_get("paused", False):
            try:
                for expr in rules.get("searches", []):
                    n = await ingest(await upwork.search(expr, rules))
                    if n:
                        log.info("search %r: %d new", expr, n)
                failures = 0
                db.kv_set("last_poll_ok", time.time())
            except Exception as e:  # noqa: BLE001
                failures += 1
                log.exception("poll failed")
                if failures == 3:
                    await telegram.send(f"⚠️ Upwork polling failed 3 times in a row: {html.escape(str(e))[:300]}")
        await asyncio.sleep(max(45, int(rules.get("poll_seconds", 75))))


async def telegram_loop() -> None:
    offset = db.kv_get("tg_offset", 0)
    while telegram.configured():
        try:
            body = await telegram.call("getUpdates", offset=offset, timeout=50,
                                       allowed_updates=["message", "callback_query"])
            for u in body.get("result", []):
                offset = u["update_id"] + 1
                db.kv_set("tg_offset", offset)
                await handle_update(u)
        except Exception:  # noqa: BLE001
            log.exception("telegram loop")
            await asyncio.sleep(5)


async def maintenance_loop() -> None:
    while True:
        purged = db.purge_old_jobs()
        if purged:
            log.info("purged %d job records older than 24h", purged)
        now = dt.datetime.now()
        today = now.date().isoformat()
        if now.hour >= load_rules().get("digest_hour", 8) and db.kv_get("digest_sent") != today:
            db.kv_set("digest_sent", today)
            await send_digest()
        due = [f for f in db.kv_get("followups", []) if f["at"] <= time.time()]
        if due:
            db.kv_set("followups", [f for f in db.kv_get("followups", []) if f["at"] > time.time()])
            for f in due:
                await telegram.send(f"How did this one go?\n<b>{html.escape(f['title'])}</b>",
                                    telegram.outcome_buttons(f["job_id"]))
        await asyncio.sleep(600)


async def send_digest() -> None:
    day = db.counts_since(time.time() - 86400)
    bands = db.score_band_stats(time.time() - 28 * 86400)
    lines = ["<b>Daily digest</b> (last 24h)",
             f"Seen {day.get('seen', 0)} · filtered out {day.get('filtered', 0)} · scored {day.get('scored', 0)} · "
             f"drafted {day.get('drafted', 0)} · approved {day.get('approved', 0)} · sent {day.get('sent', 0)} · "
             f"skipped {day.get('skipped', 0)} · errors {day.get('error', 0)}"]
    if bands:
        lines.append("\n<b>Last 28 days by score</b>")
        for b in bands:
            rate = f"{100 * b['replied'] / b['sent']:.0f}% reply" if b["sent"] else "—"
            lines.append(f"{b['band']}: {b['scored']} scored, {b['sent']} sent, {rate}, {b['hired']} hired")
    lines.append(f"\nThreshold {load_rules().get('score_threshold', 70)} · change with /threshold N")
    await telegram.send("\n".join(lines))


# ---------------------------------------------------------------- telegram handlers

async def handle_update(u: dict) -> None:
    if "callback_query" in u:
        q = u["callback_query"]
        if str(q["message"]["chat"]["id"]) != str(TELEGRAM_CHAT_ID):
            return
        await telegram.call("answerCallbackQuery", callback_query_id=q["id"])
        await handle_button(q["data"])
        return
    m = u.get("message") or {}
    chat, text = str(m.get("chat", {}).get("id")), (m.get("text") or "").strip()
    if text.startswith("/start"):
        await telegram.send(f"Hi. Your chat ID is <code>{chat}</code>. Paste it into Render as TELEGRAM_CHAT_ID.",
                            chat_id=chat)
        return
    if chat != str(TELEGRAM_CHAT_ID):
        return
    if text.startswith("/threshold"):
        rules = load_rules()
        try:
            rules["score_threshold"] = int(text.split()[1])
            save_rules(rules)
            await telegram.send(f"Threshold set to {rules['score_threshold']}.")
        except (IndexError, ValueError):
            await telegram.send("Usage: /threshold 75")
    elif text in ("/pause", "/resume"):
        db.kv_set("paused", text == "/pause")
        await telegram.send("Paused." if text == "/pause" else "Resumed.")
    elif text == "/status":
        last = db.kv_get("last_poll_ok")
        ago = f"{int(time.time() - last)}s ago" if last else "never"
        await telegram.send(f"Upwork {'connected' if upwork.connected() else 'NOT connected'} · last poll {ago} · "
                            f"{'paused' if db.kv_get('paused') else 'running'} · drafts today {drafts_today()}")
    elif text == "/digest":
        await send_digest()
    elif (job_id := db.kv_get("pending_edit")):
        db.kv_set("pending_edit", None)
        await redraft(job_id, text)
    else:
        await telegram.send("Commands: /status /digest /threshold N /pause /resume. Tap Edit on a draft to revise it.")


async def redraft(job_id: str, instruction: str) -> None:
    job, scored, prev = db.get_job(job_id), db.latest(job_id, "scored"), db.latest(job_id, "drafted")
    if not (job and scored and prev):
        await telegram.send("That job's details have expired (24h rule). Open it on Upwork directly.")
        return
    lessons = db.kv_get("edit_lessons", [])
    db.kv_set("edit_lessons", (lessons + [instruction])[-30:])
    await telegram.send("Redrafting…")
    drafted = await ai.draft(job, scored, instructions=instruction, previous=prev.get("proposal"))
    db.log(job_id, "edited", {"instruction": instruction})
    db.log(job_id, "drafted", drafted)
    await telegram.send(telegram.job_card(job, scored, drafted), telegram.decision_buttons(job_id))


async def handle_button(data: str) -> None:
    kind, _, rest = data.partition(":")
    if kind == "oc":
        result, _, job_id = rest.partition(":")
        db.log(job_id, "outcome", {"result": result})
        await telegram.send(f"Logged: {result}.")
        return
    job_id = rest
    job, drafted = db.get_job(job_id), db.latest(job_id, "drafted")
    if kind == "ap":
        db.log(job_id, "approved")
        url = job["url"] if job else f"https://www.upwork.com/jobs/{job_id}"
        await telegram.send(f'<a href="{html.escape(url)}">Open the job on Upwork</a> · bid '
                            f'{html.escape(str((drafted or {}).get("bid")))}\nTap the text below to copy it:')
        await telegram.send(f"<code>{html.escape((drafted or {}).get('proposal', ''))}</code>",
                            [[("📨 I sent it", f"st:{job_id}")]])
    elif kind == "ed":
        db.kv_set("pending_edit", job_id)
        await telegram.send("What should change? Reply in plain words, e.g. <i>shorter, lead with the Shopify result</i>.")
    elif kind == "sk":
        db.log(job_id, "skipped")
        await telegram.send("Skipped.")
    elif kind == "st":
        db.log(job_id, "sent")
        days = load_rules().get("followup_days", 5)
        db.kv_set("followups", db.kv_get("followups", []) + [
            {"job_id": job_id, "title": (job or {}).get("title", job_id), "at": time.time() + days * 86400}])
        await telegram.send(f"Logged as sent. I'll ask how it went in {days} days.")


# ---------------------------------------------------------------- web

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    tasks = [asyncio.create_task(poll_loop()), asyncio.create_task(maintenance_loop())]
    if telegram.configured():
        tasks.append(asyncio.create_task(telegram_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    if not DASHBOARD_PASSWORD or not secrets.compare_digest(creds.password.encode(), DASHBOARD_PASSWORD.encode()):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@app.get("/health")
def health():
    return {"ok": True, "upwork_connected": upwork.connected(), "telegram": telegram.configured(),
            "claude": ai.client is not None, "last_poll_ok": db.kv_get("last_poll_ok")}


@app.get("/oauth/upwork/start", dependencies=[Depends(auth)])
def oauth_start():
    if not upwork.configured():
        raise HTTPException(400, "Set UPWORK_CLIENT_ID and UPWORK_CLIENT_SECRET in Render first.")
    return RedirectResponse(upwork.authorize_url())


@app.get("/oauth/upwork/callback")
async def oauth_callback(code: str):
    await upwork.exchange_code(code)
    await telegram.send("✅ Upwork connected. Polling starts within a minute.")
    return RedirectResponse("/")


@app.post("/ingest/{secret}")
async def ingest_webhook(secret: str, request: Request):
    """Fallback feed: any alert service that can POST JSON (one job or a list) lands here."""
    if not INGEST_WEBHOOK_SECRET or not secrets.compare_digest(secret, INGEST_WEBHOOK_SECRET):
        raise HTTPException(404)
    body = await request.json()
    items = body if isinstance(body, list) else body.get("jobs", [body])
    return {"new": await ingest([upwork.normalize(j, source="webhook") for j in items])}


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Upwork Autopilot</title><style>
body{{font:14px/1.45 system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1d1d1f;background:#fafaf9}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:28px}} table{{border-collapse:collapse;width:100%}}
td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #e5e5e2;vertical-align:top}} th{{font-weight:600}}
.pill{{display:inline-block;padding:1px 8px;border-radius:9px;background:#eee}} .ok{{background:#e5f4e4}} .bad{{background:#fbebeb}}
textarea{{width:100%;font:12px ui-monospace,monospace}} button,a.btn{{padding:6px 12px;border-radius:6px;border:1px solid #ccc;background:#fff;cursor:pointer;text-decoration:none;color:inherit}}
@media(prefers-color-scheme:dark){{body{{background:#1c1c1e;color:#eee}} td,th{{border-color:#333}} .pill{{background:#333}} .ok{{background:#1f3a1f}} .bad{{background:#3a1f1f}} textarea,button,a.btn{{background:#2c2c2e;color:#eee;border-color:#444}}}}
</style>{body}""")


@app.get("/", dependencies=[Depends(auth)])
def dashboard():
    e = html.escape
    up, last = upwork.connected(), db.kv_get("last_poll_ok")
    status = (f'<span class="pill {"ok" if up else "bad"}">Upwork {"connected" if up else "not connected"}</span> '
              f'<span class="pill {"ok" if telegram.configured() else "bad"}">Telegram</span> '
              f'<span class="pill {"ok" if ai.client else "bad"}">Claude</span> '
              f'<span class="pill">last poll {f"{int(time.time() - last)}s ago" if last else "never"}</span>')
    connect = '' if up else ' <a class="btn" href="/oauth/upwork/start">Connect Upwork</a>'
    c = db.counts_since(time.time() - 86400)
    rows = []
    for r in db.recent(80):
        s = r.get("scored", {})
        stage = r.get("last_stage", "seen")
        detail = r.get("filtered", {}).get("reason") or s.get("reason") or r.get("error", {}).get("error") or ""
        rows.append(f"<tr><td>{e(r['title'] or '')}</td><td>{s.get('score', '')}</td><td>{e(stage)}</td>"
                    f"<td>{e(detail)}</td></tr>")
    rules = e(RULES_PATH.read_text())
    kfiles = "".join(f'<li><a href="/knowledge/{e(str(p.relative_to(KNOWLEDGE_DIR)))}">{e(str(p.relative_to(KNOWLEDGE_DIR)))}</a></li>'
                     for p in sorted(KNOWLEDGE_DIR.rglob("*.md")))
    return _page(f"""<h1>Upwork Autopilot</h1><p>{status}{connect}</p>
<p>Last 24h: seen {c.get('seen', 0)} · filtered {c.get('filtered', 0)} · scored {c.get('scored', 0)} ·
drafted {c.get('drafted', 0)} · approved {c.get('approved', 0)} · sent {c.get('sent', 0)} · errors {c.get('error', 0)}</p>
<h2>Recent jobs</h2><table><tr><th>Job</th><th>Score</th><th>Stage</th><th>Why</th></tr>{''.join(rows)}</table>
<h2>Rules</h2><form method=post action=/rules><textarea name=rules rows=28>{rules}</textarea><p><button>Save rules</button></p></form>
<h2>Knowledge base</h2><ul>{kfiles}</ul><form method=post action=/knowledge/new><input name=path placeholder="case-studies/new-project.md"> <button>Add file</button></form>""")


@app.post("/rules", dependencies=[Depends(auth)])
def update_rules(rules: str = Form(...)):
    import yaml
    try:
        save_rules(yaml.safe_load(rules))
    except yaml.YAMLError as err:
        raise HTTPException(400, f"Invalid YAML: {err}") from err
    return RedirectResponse("/", status_code=303)


def _kpath(path: str):
    p = (KNOWLEDGE_DIR / path).resolve()
    if not str(p).startswith(str(KNOWLEDGE_DIR.resolve())) or p.suffix != ".md":
        raise HTTPException(400, "Knowledge files must be .md inside the knowledge folder.")
    return p


@app.get("/knowledge/{path:path}", dependencies=[Depends(auth)])
def knowledge_get(path: str):
    p = _kpath(path)
    text = p.read_text() if p.exists() else ""
    return _page(f"""<p><a href="/">← back</a></p><h1>{html.escape(path)}</h1>
<form method=post><textarea name=text rows=34>{html.escape(text)}</textarea><p><button>Save</button></p></form>""")


@app.post("/knowledge/new", dependencies=[Depends(auth)])
def knowledge_new(path: str = Form(...)):
    p = _kpath(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return RedirectResponse(f"/knowledge/{path}", status_code=303)


@app.post("/knowledge/{path:path}", dependencies=[Depends(auth)])
def knowledge_save(path: str, text: str = Form(...)):
    p = _kpath(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return RedirectResponse(f"/knowledge/{path}", status_code=303)
