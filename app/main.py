"""Upwork Autopilot: one always-on service.

Loops: poll Upwork -> filter -> score -> draft -> Slack card; hourly maintenance (24h purge,
8am digest, outcome follow-ups). Slack buttons and /autopilot come back through /slack/*.
Plus a small password-protected dashboard.
"""
import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import ai, db, filters, slack, upwork
from .config import ANTHROPIC_API_KEY, DASHBOARD_PASSWORD, INGEST_WEBHOOK_SECRET, KNOWLEDGE_DIR, RULES_PATH, load_rules, save_rules

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
        await slack.post(job["title"], slack.job_blocks(job, None, None))
        return
    try:
        async with DRAFT_SLOTS:
            scored = await ai.score(job)
            db.log(job["id"], "scored", scored)
            if scored["score"] < rules.get("score_threshold", 70):
                return
            if drafts_today() >= rules.get("max_drafts_per_day", 40):
                await slack.post(job["title"], slack.job_blocks(job, scored, None) +
                                 [slack._text("_Daily draft cap reached; not drafted._")])
                return
            drafted = await ai.draft(job, scored)
            db.log(job["id"], "drafted", drafted)
        await slack.post(f"Score {scored['score']}: {job['title']}", slack.job_blocks(job, scored, drafted))
    except Exception as e:  # noqa: BLE001 — one bad job must never stop the loop
        log.exception("process failed for %s", job["id"])
        db.log(job["id"], "error", {"error": str(e)[:500]})


async def try_job(text: str) -> None:
    """Score and draft a pasted job post regardless of filters and threshold, so you can judge the output.
    No budget or client data comes with a paste, so those show as unknown."""
    text = text.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    titled = [ln[6:].strip() for ln in lines if ln.lower().startswith("title:")]
    body = [ln for ln in lines if ln.lower() not in ("summary", "summary:", "description", "description:")]
    title = (titled[0] if titled else body[0] if body else "Pasted job")[:120]
    job = upwork.normalize({"id": "try-" + hashlib.sha1(text.encode()).hexdigest()[:10], "title": title,
                            "description": text, "url": "https://www.upwork.com/nx/find-work/"}, source="manual")
    db.mark_seen(job["id"], "manual")
    db.save_job(job)
    try:
        scored = await ai.score(job)
        db.log(job["id"], "scored", scored)
        drafted = await ai.draft(job, scored)
        db.log(job["id"], "drafted", drafted)
        note = "" if scored["score"] >= load_rules().get("score_threshold", 70) else \
            "\n_Below your threshold: live, this job would be logged but not drafted._"
        blocks = slack.job_blocks(job, scored, drafted, decide=False)
        blocks.insert(0, slack._text(f":test_tube: *Test run on a pasted post* (budget and client unknown){note}"))
        await slack.post(f"Test: {title}", blocks)
    except Exception as e:  # noqa: BLE001
        log.exception("try_job failed")
        db.log(job["id"], "error", {"error": str(e)[:500]})
        await slack.post(f":warning: Test run failed: {str(e)[:300]}")


async def check_claude_key(notify: bool = True) -> None:
    """Free call that proves the API key works, so a bad key shows up at boot, not on the first job."""
    if not ai.client:
        db.kv_set("claude_key_check", "missing")
        return
    try:
        await ai.client.models.list(limit=1)
        db.kv_set("claude_key_check", "ok")
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status_code", None)
        result = "invalid (Anthropic rejected the key)" if status == 401 else f"error: {str(e)[:120]}"
        db.kv_set("claude_key_check", result)
        if notify:
            await slack.post(f":warning: Claude API key check failed: {result}. "
                             "Paste a fresh key into Render → Environment → ANTHROPIC_API_KEY.")


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
                    await slack.post(f":warning: Upwork polling failed 3 times in a row: {str(e)[:300]}")
        await asyncio.sleep(max(45, int(rules.get("poll_seconds", 75))))


async def maintenance_loop() -> None:
    while True:
        try:
            purged = db.purge_old_jobs()
            if purged:
                log.info("purged %d job records older than 24h", purged)
            now = dt.datetime.now()
            today = now.date().isoformat()
            if now.hour >= load_rules().get("digest_hour", 8) and db.kv_get("digest_sent") != today:
                db.kv_set("digest_sent", today)
                await send_digest()
            pending = db.kv_get("followups", [])
            due = [f for f in pending if f["at"] <= time.time()]
            if due:
                db.kv_set("followups", [f for f in pending if f["at"] > time.time()])
                for f in due:
                    await slack.post("How did this one go?", slack.outcome_blocks(f["job_id"], f["title"]))
        except Exception:  # noqa: BLE001
            log.exception("maintenance")
        await asyncio.sleep(600)


def digest_text() -> str:
    day = db.counts_since(time.time() - 86400)
    bands = db.score_band_stats(time.time() - 28 * 86400)
    lines = ["*Daily digest* (last 24h)",
             f"Seen {day.get('seen', 0)} · filtered out {day.get('filtered', 0)} · scored {day.get('scored', 0)} · "
             f"drafted {day.get('drafted', 0)} · approved {day.get('approved', 0)} · sent {day.get('sent', 0)} · "
             f"skipped {day.get('skipped', 0)} · errors {day.get('error', 0)}"]
    if bands:
        lines.append("\n*Last 28 days by score*")
        for b in bands:
            rate = f"{100 * b['replied'] / b['sent']:.0f}% reply" if b["sent"] else "—"
            lines.append(f"{b['band']}: {b['scored']} scored, {b['sent']} sent, {rate}, {b['hired']} hired")
    lines.append(f"\nThreshold {load_rules().get('score_threshold', 70)} · change with `/autopilot threshold 75`")
    return "\n".join(lines)


async def send_digest() -> None:
    await slack.post("Daily digest", [slack._text(digest_text())])


# ---------------------------------------------------------------- slack actions

async def on_action(payload: dict) -> None:
    act = payload["actions"][0]
    action, job_id = act["action_id"], act.get("value", "")
    channel = payload["channel"]["id"]
    ts = payload["container"]["message_ts"]
    blocks = payload["message"].get("blocks", [])
    thread = payload["message"].get("thread_ts") or ts
    job, drafted = db.get_job(job_id), db.latest(job_id, "drafted")

    if action == "approve":
        db.log(job_id, "approved")
        url = job["url"] if job else f"https://www.upwork.com/jobs/{job_id}"
        await slack.mark(channel, ts, blocks, ":white_check_mark: Approved")
        await slack.post("Ready to send", slack.approved_blocks(job_id, url, drafted or {}), thread_ts=thread)
    elif action == "edit":
        await slack.call("views.open", trigger_id=payload["trigger_id"], view=slack.edit_modal(job_id, channel, ts))
    elif action == "skip":
        db.log(job_id, "skipped")
        await slack.mark(channel, ts, blocks, ":fast_forward: Skipped")
    elif action == "sent":
        db.log(job_id, "sent")
        days = load_rules().get("followup_days", 5)
        db.kv_set("followups", db.kv_get("followups", []) + [
            {"job_id": job_id, "title": (job or {}).get("title", job_id), "at": time.time() + days * 86400}])
        await slack.call("chat.update", channel=channel, ts=ts, text="Sent",
                         blocks=[b for b in blocks if b.get("type") != "actions"] + [
                             {"type": "context", "elements": [{"type": "mrkdwn",
                              "text": f":outbox_tray: Sent. I'll ask how it went in {days} days."}]}])
    elif action.startswith("outcome_"):
        result = action.removeprefix("outcome_")
        db.log(job_id, "outcome", {"result": result})
        await slack.call("chat.update", channel=channel, ts=ts, text=f"Logged: {result}",
                         blocks=[blocks[0], {"type": "context", "elements": [
                             {"type": "mrkdwn", "text": f"Logged: *{result}*"}]}])


async def on_redraft(job_id: str, channel: str, ts: str, instruction: str) -> None:
    job, scored, prev = db.get_job(job_id), db.latest(job_id, "scored"), db.latest(job_id, "drafted")
    if not (job and scored and prev):
        await slack.post("That job's details have expired (24h rule). Open it on Upwork directly.", thread_ts=ts)
        return
    db.kv_set("edit_lessons", (db.kv_get("edit_lessons", []) + [instruction])[-30:])
    drafted = await ai.draft(job, scored, instructions=instruction, previous=prev.get("proposal"))
    db.log(job_id, "edited", {"instruction": instruction})
    db.log(job_id, "drafted", drafted)
    await slack.post(f"Revised: {job['title']}", slack.job_blocks(job, scored, drafted), thread_ts=ts)


def command_reply(text: str) -> str:
    args = text.split()
    cmd = args[0].lower() if args else "help"
    if cmd == "threshold" and len(args) == 2 and args[1].isdigit():
        rules = load_rules()
        rules["score_threshold"] = int(args[1])
        save_rules(rules)
        return f"Threshold set to {args[1]}."
    if cmd in ("pause", "resume"):
        db.kv_set("paused", cmd == "pause")
        return "Paused." if cmd == "pause" else "Resumed."
    if cmd == "status":
        last = db.kv_get("last_poll_ok")
        ago = f"{int(time.time() - last)}s ago" if last else "never"
        return (f"Upwork {'connected' if upwork.connected() else '*not connected*'} · last poll {ago} · "
                f"{'paused' if db.kv_get('paused') else 'running'} · drafts today {drafts_today()} · "
                f"threshold {load_rules().get('score_threshold', 70)}")
    if cmd == "digest":
        return digest_text()
    return ("`/autopilot status` · `digest` · `threshold 75` · `pause` · `resume` · "
            "`try <paste a job post>` (scores and drafts it in the channel)")


# ---------------------------------------------------------------- web

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    tasks = [asyncio.create_task(poll_loop()), asyncio.create_task(maintenance_loop()),
             asyncio.create_task(check_claude_key())]
    if slack.configured():
        asyncio.create_task(slack.post(":rocket: Upwork Autopilot is running. Try `/autopilot status`."))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    if not DASHBOARD_PASSWORD or not secrets.compare_digest(creds.password.encode(), DASHBOARD_PASSWORD.encode()):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@app.get("/health")
async def health():
    # re-test a failing Claude key at most every 30s (listing models is free), so fixes show up without a redeploy
    if db.kv_get("claude_key_check") != "ok" and time.time() - db.kv_get("claude_key_checked_at", 0) > 30:
        db.kv_set("claude_key_checked_at", time.time())
        await check_claude_key(notify=False)
    return {"ok": True, "upwork_connected": upwork.connected(), "slack": slack.configured(),
            "claude": ai.client is not None, "last_poll_ok": db.kv_get("last_poll_ok"),
            "knowledge_files": sorted(str(p.relative_to(KNOWLEDGE_DIR)) for p in KNOWLEDGE_DIR.rglob("*.md")),
            "min_hourly": load_rules().get("filters", {}).get("min_hourly"),
            "searches": len(load_rules().get("searches", [])),
            "claude_key": db.kv_get("claude_key_check", "not checked"),
            "claude_key_shape": _key_shape()}


def _key_shape() -> str:
    """Safe fingerprint: how the key starts, its length, and characters that don't belong. Never the key."""
    k = ANTHROPIC_API_KEY
    if not k:
        return "empty"
    odd = sorted({c for c in k if not (c.isalnum() or c in "-_")})
    return f"starts '{k[:10]}', {len(k)} chars" + (f", unexpected characters {odd}" if odd else "")


@app.post("/slack/interact")
async def slack_interact(request: Request, bg: BackgroundTasks):
    body = await request.body()
    if not slack.verify(request.headers, body):
        raise HTTPException(401)
    payload = json.loads(parse_qs(body.decode())["payload"][0])
    if payload["type"] == "block_actions":
        bg.add_task(on_action, payload)  # Slack needs an answer within 3s; do the work after
    elif payload["type"] == "view_submission" and payload["view"]["callback_id"] == "redraft":
        job_id, channel, ts = payload["view"]["private_metadata"].split("|")
        text = payload["view"]["state"]["values"]["instr"]["text"]["value"]
        bg.add_task(on_redraft, job_id, channel, ts, text)
    return Response(status_code=200)


@app.post("/slack/command")
async def slack_command(request: Request):
    body = await request.body()
    if not slack.verify(request.headers, body):
        raise HTTPException(401)
    form = parse_qs(body.decode())
    text = form.get("text", [""])[0]
    if text.lower().startswith("try"):
        post = text[3:].strip()
        if len(post) < 40:
            return JSONResponse({"response_type": "ephemeral", "text": "Paste the whole job post after `try`."})
        asyncio.create_task(try_job(post))
        return JSONResponse({"response_type": "ephemeral", "text": "Scoring and drafting… the card lands in the channel in ~30s."})
    return JSONResponse({"response_type": "ephemeral", "text": command_reply(text)})


@app.post("/try", dependencies=[Depends(auth)])
async def try_form(post: str = Form(...)):
    asyncio.create_task(try_job(post))
    return RedirectResponse("/?tried=1", status_code=303)


@app.get("/oauth/upwork/start", dependencies=[Depends(auth)])
def oauth_start():
    if not upwork.configured():
        raise HTTPException(400, "Set UPWORK_CLIENT_ID and UPWORK_CLIENT_SECRET in Render first.")
    return RedirectResponse(upwork.authorize_url())


@app.get("/oauth/upwork/callback")
async def oauth_callback(code: str):
    await upwork.exchange_code(code)
    await slack.post(":white_check_mark: Upwork connected. Polling starts within a minute.")
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
              f'<span class="pill {"ok" if slack.configured() else "bad"}">Slack</span> '
              f'<span class="pill {"ok" if db.kv_get("claude_key_check") == "ok" else "bad"}">Claude key: {e(str(db.kv_get("claude_key_check", "not checked")))}</span> '
              f'<span class="pill">key {e(_key_shape())}, ends …{e(ANTHROPIC_API_KEY[-4:])}</span> '
              f'<span class="pill">last poll {f"{int(time.time() - last)}s ago" if last else "never"}</span>')
    connect = '' if up else ' <a class="btn" href="/oauth/upwork/start">Connect Upwork</a>'
    c = db.counts_since(time.time() - 86400)
    rows = []
    for r in db.recent(80):
        s = r.get("scored", {})
        detail = r.get("filtered", {}).get("reason") or s.get("reason") or r.get("error", {}).get("error") or ""
        rows.append(f"<tr><td>{e(r['title'] or '')}</td><td>{s.get('score', '')}</td>"
                    f"<td>{e(r.get('last_stage', 'seen'))}</td><td>{e(detail)}</td></tr>")
    kfiles = "".join(f'<li><a href="/knowledge/{e(str(p.relative_to(KNOWLEDGE_DIR)))}">{e(str(p.relative_to(KNOWLEDGE_DIR)))}</a></li>'
                     for p in sorted(KNOWLEDGE_DIR.rglob("*.md")))
    return _page(f"""<h1>Upwork Autopilot</h1><p>{status}{connect}</p>
<p>Last 24h: seen {c.get('seen', 0)} · filtered {c.get('filtered', 0)} · scored {c.get('scored', 0)} ·
drafted {c.get('drafted', 0)} · approved {c.get('approved', 0)} · sent {c.get('sent', 0)} · errors {c.get('error', 0)}</p>
<h2>Try a job post</h2><form method=post action=/try><textarea name=post rows=6 placeholder="Paste any Upwork job post. The score and draft land in Slack in ~30 seconds."></textarea><p><button>Score and draft it</button></p></form>
<h2>Recent jobs</h2><table><tr><th>Job</th><th>Score</th><th>Stage</th><th>Why</th></tr>{''.join(rows)}</table>
<h2>Rules</h2><form method=post action=/rules><input type=hidden name=base value="{_rules_sha()}"><textarea name=rules rows=28>{e(RULES_PATH.read_text())}</textarea><p><button>Save rules</button></p></form>
<h2>Knowledge base</h2><ul>{kfiles}</ul><form method=post action=/knowledge/new><input name=path placeholder="case-studies/new-project.md"> <button>Add file</button></form>""")


def _rules_sha() -> str:
    return hashlib.sha256(RULES_PATH.read_bytes()).hexdigest()[:16]


@app.post("/rules", dependencies=[Depends(auth)])
def update_rules(rules: str = Form(...), base: str = Form("")):
    import yaml
    if base != _rules_sha():
        return _page('<p>The rules changed since you opened this page, so nothing was saved. '
                     '<a href="/">Reload the dashboard</a> and make your edit again.</p>')
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
