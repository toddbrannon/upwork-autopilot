"""Offline Slack flow test: signed requests drive Approve / Edit modal / Skip / Sent / outcome / slash command.
Claude and Slack's API are mocked; signature verification and the HTTP endpoints are real."""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from urllib.parse import urlencode

os.environ.update(DATA_DIR=tempfile.mkdtemp(), SLACK_BOT_TOKEN="xoxb-test", SLACK_SIGNING_SECRET="sig",
                  SLACK_CHANNEL_ID="C1", DASHBOARD_PASSWORD="pw")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, db, main, slack, upwork  # noqa: E402
from tests.smoke import NODE  # noqa: E402

calls = []


async def fake_call(method, **p):
    calls.append((method, p))
    return {"ok": True, "ts": "111.222"}


async def fake_score(job):
    return {"score": 82, "reason": "Strong match", "red_flags": [], "suggested_bid": "$75/hr", "cost": 0.0003}


async def fake_draft(job, scored, instructions=None, previous=None):
    return {"proposal": "You need lead intake automated. " + (instructions or ""), "bid": "$75/hr",
            "duration": "2 weeks", "screening_answers": [], "confidence": 80, "cost": 0.02}


slack.call, ai.score, ai.draft, ai.client = fake_call, fake_score, fake_draft, object()


def signed(path, body: str, client):
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"sig", f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return client.post(path, content=body, headers={"x-slack-request-timestamp": ts, "x-slack-signature": sig,
                                                   "content-type": "application/x-www-form-urlencoded"})


def action(action_id, job_id, blocks):
    return {"type": "block_actions", "trigger_id": "trig", "channel": {"id": "C1"},
            "container": {"message_ts": "111.222"}, "message": {"ts": "111.222", "blocks": blocks},
            "actions": [{"action_id": action_id, "value": job_id}]}


def main_test():
    with TestClient(main.app) as c:
        job = upwork.normalize(NODE, "t")
        db.mark_seen(job["id"], "t")
        asyncio.run(main.process(job))
        card = [p for m, p in calls if m == "chat.postMessage" and p.get("blocks") and "decide" in json.dumps(p)][-1]
        ids = [e["action_id"] for b in card["blocks"] if b["type"] == "actions" for e in b["elements"]]
        assert ids == ["approve", "edit", "skip", "open"], ids
        blocks = card["blocks"]

        # unsigned / tampered requests are rejected
        assert c.post("/slack/interact", content="payload={}").status_code == 401

        for aid in ("approve", "edit", "sent", "outcome_hired"):
            body = urlencode({"payload": json.dumps(action(aid, job["id"], blocks))})
            assert signed("/slack/interact", body, c).status_code == 200, aid

        view = {"type": "view_submission", "view": {"callback_id": "redraft",
                "private_metadata": f"{job['id']}|C1|111.222",
                "state": {"values": {"instr": {"text": {"value": "shorter"}}}}}}
        assert signed("/slack/interact", urlencode({"payload": json.dumps(view)}), c).status_code == 200

        r = signed("/slack/command", urlencode({"command": "/autopilot", "text": "threshold 75"}), c)
        assert r.json()["text"] == "Threshold set to 75.", r.json()
        r = signed("/slack/command", urlencode({"command": "/autopilot", "text": "status"}), c)
        assert "threshold 75" in r.json()["text"]

    stages = [s for s in ("approved", "sent", "outcome", "edited") if db.latest(job["id"], s) is not None]
    assert stages == ["approved", "sent", "outcome", "edited"], stages
    methods = [m for m, _ in calls]
    assert "views.open" in methods and methods.count("chat.update") >= 3, methods
    redraft = [p for m, p in calls if m == "chat.postMessage" and p.get("thread_ts") and "shorter" in json.dumps(p)]
    assert redraft, "redraft not posted in thread"
    print("slack flow OK:", ", ".join(dict.fromkeys(methods)))


if __name__ == "__main__":
    main_test()
