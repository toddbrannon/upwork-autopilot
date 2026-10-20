"""Telegram bot: alerts with Approve / Edit / Skip, long-polled so no webhook setup is needed."""
import html
import logging

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("telegram")
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


async def call(method: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=70) as h:
        r = await h.post(f"{API}/{method}", json=params)
    body = r.json()
    if not body.get("ok"):
        log.warning("telegram %s failed: %s", method, body)
    return body


async def send(text: str, buttons: list[list[tuple[str, str]]] | None = None, chat_id: str | None = None) -> dict:
    params = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text[:4096], "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                      for row in buttons]}
    return await call("sendMessage", **params)


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _budget(job: dict) -> str:
    if job.get("job_type") == "HOURLY":
        lo, hi = job.get("hourly_min"), job.get("hourly_max")
        return f"${lo:.0f}–{hi:.0f}/hr" if lo and hi else f"${(hi or lo or 0):.0f}/hr" if (hi or lo) else "hourly"
    return f"${job['budget_fixed']:,.0f} fixed" if job.get("budget_fixed") else "budget n/a"


def job_card(job: dict, scored: dict | None, drafted: dict | None) -> str:
    c = job["client"]
    lines = [f"<b>{esc(job['title'])}</b>",
             f"{_budget(job)} · {esc(job.get('proposals') or 0)} proposals · "
             f"{'✓ verified' if c.get('payment_verified') else 'unverified'} · "
             f"{esc(c.get('hires') or 0)} hires · ${(c.get('spent') or 0):,.0f} spent · {esc(c.get('country') or '')}"]
    if scored:
        flags = f"  ⚠ {esc(', '.join(scored['red_flags']))}" if scored.get("red_flags") else ""
        lines.append(f"\n<b>Score {scored['score']}</b> — {esc(scored.get('reason'))}{flags}")
    if drafted:
        lines.append(f"\n<b>Bid</b> {esc(drafted.get('bid'))} · {esc(drafted.get('duration'))}")
        lines.append(f"\n<pre>{esc(drafted.get('proposal'))}</pre>")
        for a in drafted.get("screening_answers") or []:
            lines.append(f"<i>Q:</i> {esc(a)}")
    lines.append(f'\n<a href="{esc(job["url"])}">Open on Upwork</a>')
    return "\n".join(lines)


def decision_buttons(job_id: str) -> list[list[tuple[str, str]]]:
    return [[("✅ Approve", f"ap:{job_id}"), ("✏️ Edit", f"ed:{job_id}"), ("⏭ Skip", f"sk:{job_id}")]]


def outcome_buttons(job_id: str) -> list[list[tuple[str, str]]]:
    return [[("No reply", f"oc:none:{job_id}"), ("Replied", f"oc:replied:{job_id}")],
            [("Interview", f"oc:interviewed:{job_id}"), ("Hired 🎉", f"oc:hired:{job_id}")]]
