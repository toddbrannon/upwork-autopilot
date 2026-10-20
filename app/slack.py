"""Slack app: job cards with Approve / Edit / Skip, a redraft modal, outcome check-ins, /autopilot command.

Slack calls us at /slack/interact (buttons, modal) and /slack/command (slash command);
every request is verified with the app's signing secret.
"""
import hashlib
import hmac
import logging
import time

import httpx

from .config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, SLACK_SIGNING_SECRET

log = logging.getLogger("slack")
API = "https://slack.com/api"


def configured() -> bool:
    return bool(SLACK_BOT_TOKEN and SLACK_CHANNEL_ID)


def verify(headers, body: bytes) -> bool:
    ts = headers.get("x-slack-request-timestamp", "0")
    sig = headers.get("x-slack-signature", "")
    if not SLACK_SIGNING_SECRET or abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:".encode() + body
    mine = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig)


async def call(method: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=20) as h:
        r = await h.post(f"{API}/{method}", json=params,
                         headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    body = r.json()
    if not body.get("ok"):
        log.warning("slack %s failed: %s", method, body.get("error"))
    return body


async def post(text: str, blocks: list | None = None, thread_ts: str | None = None) -> dict:
    params = {"channel": SLACK_CHANNEL_ID, "text": text[:3000], "unfurl_links": False}
    if blocks:
        params["blocks"] = blocks
    if thread_ts:
        params["thread_ts"] = thread_ts
    return await call("chat.postMessage", **params)


# ---------------------------------------------------------------- blocks

def esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _budget(job: dict) -> str:
    if job.get("job_type") == "HOURLY":
        lo, hi = job.get("hourly_min"), job.get("hourly_max")
        return f"${lo:.0f}–{hi:.0f}/hr" if lo and hi else f"${(hi or lo):.0f}/hr" if (hi or lo) else "hourly"
    return f"${job['budget_fixed']:,.0f} fixed" if job.get("budget_fixed") else "budget n/a"


def _text(s: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": s[:2990]}}


def _button(label: str, action_id: str, value: str, style: str | None = None, url: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    return b


def job_blocks(job: dict, scored: dict | None, drafted: dict | None, decide: bool = True) -> list:
    c = job["client"]
    blocks = [_text(f"*<{job['url']}|{esc(job['title'])}>*\n{_budget(job)} · {job.get('proposals') or 0} proposals · "
                    f"{'✓ verified' if c.get('payment_verified') else 'unverified'} · {c.get('hires') or 0} hires · "
                    f"${(c.get('spent') or 0):,.0f} spent · {esc(c.get('country') or '')}")]
    if scored:
        flags = f"\n:warning: {esc(', '.join(scored['red_flags']))}" if scored.get("red_flags") else ""
        blocks.append(_text(f"*Score {scored['score']}* — {esc(scored.get('reason'))}{flags}"))
    if drafted:
        blocks.append(_text(f"*Bid* {esc(drafted.get('bid'))} · {esc(drafted.get('duration'))}"))
        blocks.append(_text(f"```{esc(drafted.get('proposal'))}```"))
        for a in drafted.get("screening_answers") or []:
            blocks.append(_text(f"_Q:_ {esc(a)}"))
    if decide and drafted:
        blocks.append({"type": "actions", "block_id": "decide", "elements": [
            _button("Approve", "approve", job["id"], "primary"),
            _button("Edit", "edit", job["id"]),
            _button("Skip", "skip", job["id"]),
            _button("Open on Upwork", "open", job["id"], url=job["url"]),
        ]})
    return blocks


def approved_blocks(job_id: str, url: str, drafted: dict) -> list:
    return [
        _text(f"*Ready to send.* Bid {esc(drafted.get('bid'))}. Copy the text, open the job, paste, submit."),
        _text(f"```{esc(drafted.get('proposal'))}```"),
        {"type": "actions", "elements": [_button("Open on Upwork", "open", job_id, "primary", url=url),
                                         _button("I sent it", "sent", job_id)]},
    ]


def outcome_blocks(job_id: str, title: str) -> list:
    return [_text(f"How did this one go?\n*{esc(title)}*"),
            {"type": "actions", "elements": [_button("No reply", "outcome_none", job_id),
                                             _button("Replied", "outcome_replied", job_id),
                                             _button("Interview", "outcome_interviewed", job_id),
                                             _button("Hired", "outcome_hired", job_id, "primary")]}]


def edit_modal(job_id: str, channel: str, ts: str) -> dict:
    return {"type": "modal", "callback_id": "redraft", "private_metadata": f"{job_id}|{channel}|{ts}",
            "title": {"type": "plain_text", "text": "Revise the draft"},
            "submit": {"type": "plain_text", "text": "Redraft"},
            "blocks": [{"type": "input", "block_id": "instr", "label": {"type": "plain_text", "text": "What should change?"},
                        "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                                    "placeholder": {"type": "plain_text",
                                                    "text": "e.g. shorter, lead with the Shopify result"}}}]}


async def mark(channel: str, ts: str, blocks: list, status: str) -> None:
    """Replace the card's buttons with a status line so you can see what you decided."""
    kept = [b for b in blocks if b.get("block_id") != "decide"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status}]})
    await call("chat.update", channel=channel, ts=ts, blocks=kept, text=status)
