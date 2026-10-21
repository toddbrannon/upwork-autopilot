"""Gate 2 (score, Haiku) and Gate 3 (draft, Sonnet). The knowledge base is the cached system prompt,
so each call pays full price only for the job text and the output."""
import json
import logging
import re

from anthropic import AsyncAnthropic

from . import db
from .config import ANTHROPIC_API_KEY, ANTHROPIC_WORKSPACE_ID, DRAFTER_MODEL, SCORER_MODEL, load_knowledge

log = logging.getLogger("ai")


def _make_client() -> AsyncAnthropic | None:
    """Legacy workspace keys (sk-ant-api...) go in x-api-key. Personal and service-account keys
    (sk-ant-usr..., sk-ant-svc...) are sent as Authorization: Bearer, per Anthropic's auth docs.
    A key not scoped to one workspace also needs the anthropic-workspace-id header."""
    if not ANTHROPIC_API_KEY:
        return None
    headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
    if ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return AsyncAnthropic(api_key=ANTHROPIC_API_KEY, default_headers=headers)
    return AsyncAnthropic(api_key=None, auth_token=ANTHROPIC_API_KEY, default_headers=headers)


client = _make_client()

SCORER_RULES = """You screen Upwork job posts for one freelancer, whose knowledge base is above.
Score how worth bidding this job is for THEM, 0-100, with this rubric:
- 40 skills match: does their proven work (case studies, profile) directly cover what is asked?
- 20 scope clarity: clear deliverables and acceptance criteria score high; vague asks score low.
- 20 client quality: payment verified, hires, spend, rating, reasonable tone.
- 10 budget realism: budget fits the scope and the freelancer's pricing rules.
- 10 competition: fewer proposals and a fresh post score higher.
Red flags cap the score at 40: unpaid test work, asks to move off Upwork or pay outside it,
tiny budget for large scope, homework/exam help, account sharing, anything against Upwork terms.
Reply with ONLY a JSON object:
{"score": int, "reason": "one sentence a busy person reads in 3 seconds",
 "red_flags": [str], "suggested_bid": "e.g. $65/hr or $1,800 fixed"}"""

DRAFTER_RULES = """You write Upwork proposals for the freelancer whose knowledge base is above, in their voice.
Use ONLY facts from the knowledge base. Never invent clients, numbers, tools, or results.
Shape:
- 120-220 words. Plain text, short paragraphs, no headings, no emoji, no "Dear Hiring Manager".
- Open with THEIR problem in one sentence that proves you read the post. Not your bio.
- One concrete, relevant result from a case study, with its number.
- How you would approach this job in 2-3 specific steps.
- One sharp clarifying question about their project.
- A clear, low-friction next step.
Follow voice.md and pricing.md exactly. Match the patterns of the winning proposals.
If the post asks screening questions or includes a keyword to prove reading, answer/include it.
Reply with ONLY a JSON object:
{"proposal": str, "bid": str, "duration": str, "screening_answers": [str], "confidence": int 0-100}"""


def _system(rules_text: str) -> list[dict]:
    return [
        {"type": "text", "text": "<knowledge_base>\n" + load_knowledge() + "\n</knowledge_base>",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": rules_text},
    ]


def _job_text(job: dict) -> str:
    c = job["client"]
    budget = (f"${job['hourly_min'] or '?'}-{job['hourly_max'] or '?'}/hr" if job.get("job_type") == "HOURLY"
              else f"${job['budget_fixed']:.0f} fixed" if job.get("budget_fixed") else "not stated")
    return (f"TITLE: {job['title']}\nBUDGET: {budget}\nEXPERIENCE: {job.get('experience')}\n"
            f"DURATION: {job.get('duration')}\nPROPOSALS SO FAR: {job.get('proposals')}\n"
            f"SKILLS: {', '.join(job.get('skills') or [])}\n"
            f"CLIENT: verified={c.get('payment_verified')} hires={c.get('hires')} spent={c.get('spent')} "
            f"rating={c.get('rating')} country={c.get('country')}\n\nDESCRIPTION:\n{job['description']}")


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def _cost(model: str, usage) -> float:
    """Approximate USD from usage, per Anthropic list prices (Oct 2026)."""
    p = {"claude-haiku-5-5": (0.10, 0.50, 0.01, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.10, 4.0)}.get(
        model, (2.0, 10.0, 0.10, 4.0))
    return ((usage.input_tokens * p[0] + usage.output_tokens * p[1]
             + (usage.cache_read_input_tokens or 0) * p[2]
             + (usage.cache_creation_input_tokens or 0) * p[3]) / 1e6)


async def score(job: dict) -> dict:
    msg = await client.messages.create(
        model=SCORER_MODEL, max_tokens=400, system=_system(SCORER_RULES),
        messages=[{"role": "user", "content": _job_text(job)}])
    out = _json(msg.content[0].text)
    out["score"] = int(out.get("score", 0))
    out["cost"] = round(_cost(SCORER_MODEL, msg.usage), 5)
    return out


async def draft(job: dict, scored: dict, instructions: str | None = None, previous: str | None = None) -> dict:
    user = _job_text(job) + f"\n\nSCREENER'S NOTE: {scored.get('reason')} (suggested bid {scored.get('suggested_bid')})"
    lessons = db.kv_get("edit_lessons", [])[-10:]
    if lessons:
        user += "\n\nTHE FREELANCER RECENTLY ASKED FOR THESE CHANGES ON OTHER DRAFTS (apply the pattern):\n- " + "\n- ".join(lessons)
    if previous and instructions:
        user += f"\n\nYOUR PREVIOUS DRAFT:\n{previous}\n\nREVISE IT PER THE FREELANCER'S INSTRUCTION:\n{instructions}"
    msg = await client.messages.create(
        model=DRAFTER_MODEL, max_tokens=1500, system=_system(DRAFTER_RULES),
        messages=[{"role": "user", "content": user}])
    out = _json(msg.content[0].text)
    out["cost"] = round(_cost(DRAFTER_MODEL, msg.usage), 5)
    return out
