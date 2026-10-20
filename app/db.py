"""SQLite on the persistent disk. Upwork job text is purged after 24h (API terms); your own work is kept."""
import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs_seen (
  job_id TEXT PRIMARY KEY, first_seen REAL NOT NULL, source TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, fetched_at REAL NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts REAL NOT NULL,
  stage TEXT NOT NULL,          -- filtered | scored | drafted | approved | edited | skipped | sent | outcome | error
  payload TEXT
);
CREATE INDEX IF NOT EXISTS ev_job ON events(job_id);
CREATE INDEX IF NOT EXISTS ev_stage_ts ON events(stage, ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")


def mark_seen(job_id: str, source: str) -> bool:
    """Returns True if this job is new."""
    with conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO jobs_seen VALUES (?,?,?)", (job_id, time.time(), source))
        return cur.rowcount == 1


def save_job(job: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (job["id"], time.time(), json.dumps(job)))


def get_job(job_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(r["data"]) if r else None


def log(job_id: str, stage: str, payload: dict | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO events(job_id, ts, stage, payload) VALUES (?,?,?,?)",
                  (job_id, time.time(), stage, json.dumps(payload or {})))


def latest(job_id: str, stage: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT payload FROM events WHERE job_id=? AND stage=? ORDER BY id DESC LIMIT 1",
                      (job_id, stage)).fetchone()
        return json.loads(r["payload"]) if r else None


def kv_get(k: str, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))


def purge_old_jobs(max_age_s: int = 24 * 3600) -> int:
    with conn() as c:
        return c.execute("DELETE FROM jobs WHERE fetched_at < ?", (time.time() - max_age_s,)).rowcount


def counts_since(ts: float) -> dict:
    with conn() as c:
        rows = c.execute("SELECT stage, COUNT(*) n FROM events WHERE ts >= ? GROUP BY stage", (ts,)).fetchall()
        seen = c.execute("SELECT COUNT(*) n FROM jobs_seen WHERE first_seen >= ?", (ts,)).fetchone()["n"]
    out = {r["stage"]: r["n"] for r in rows}
    out["seen"] = seen
    return out


def recent(limit: int = 100) -> list[dict]:
    """One row per recent job with its latest score/decision, for the dashboard."""
    with conn() as c:
        ids = [r["job_id"] for r in c.execute(
            "SELECT job_id FROM jobs_seen ORDER BY first_seen DESC LIMIT ?", (limit,))]
        out = []
        for jid in ids:
            evs = c.execute("SELECT stage, ts, payload FROM events WHERE job_id=? ORDER BY id", (jid,)).fetchall()
            job = c.execute("SELECT data FROM jobs WHERE job_id=?", (jid,)).fetchone()
            row = {"id": jid, "title": json.loads(job["data"]).get("title") if job else "(purged after 24h)"}
            for e in evs:
                row[e["stage"]] = json.loads(e["payload"])
                row["last_stage"] = e["stage"]
            out.append(row)
        return out


def score_band_stats(since: float) -> list[dict]:
    """Win rate by score band: how many drafted jobs in each band were sent and led to a reply/hire."""
    with conn() as c:
        rows = c.execute("""
          SELECT s.job_id, json_extract(s.payload,'$.score') score,
                 EXISTS(SELECT 1 FROM events e WHERE e.job_id=s.job_id AND e.stage='sent') sent,
                 (SELECT json_extract(payload,'$.result') FROM events e WHERE e.job_id=s.job_id AND e.stage='outcome'
                  ORDER BY id DESC LIMIT 1) result
          FROM events s WHERE s.stage='scored' AND s.ts >= ?""", (since,)).fetchall()
    bands = {}
    for r in rows:
        sc = r["score"] or 0
        band = "80+" if sc >= 80 else "70-79" if sc >= 70 else "60-69" if sc >= 60 else "<60"
        b = bands.setdefault(band, {"band": band, "scored": 0, "sent": 0, "replied": 0, "hired": 0})
        b["scored"] += 1
        b["sent"] += r["sent"]
        if r["result"] in ("replied", "interviewed", "hired"):
            b["replied"] += 1
        if r["result"] == "hired":
            b["hired"] += 1
    return sorted(bands.values(), key=lambda b: b["band"], reverse=True)
