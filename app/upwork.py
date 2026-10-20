"""Upwork GraphQL client: OAuth 2.0 (authorization-code + rotating refresh token) and job search.

Docs: https://www.upwork.com/developer/documentation/graphql/api/docs/index.html
Access tokens live 24h; refresh tokens 2 weeks since last use and ROTATE on every refresh,
so both are stored on the disk (kv table), never in env vars.

The query below follows the documented `marketplaceJobPostings` search. Field names on the
job node should be confirmed in Upwork's GQL Explorer once the key is approved; the normalizer
reads every field defensively, so a missing field degrades a filter instead of crashing.
"""
import logging
import time
from urllib.parse import urlencode

import httpx

from . import db
from .config import PUBLIC_BASE_URL, UPWORK_CLIENT_ID, UPWORK_CLIENT_SECRET

log = logging.getLogger("upwork")

GQL = "https://api.upwork.com/graphql"
AUTH_URL = "https://www.upwork.com/ab/account-security/oauth2/authorize"
TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"
REDIRECT = f"{PUBLIC_BASE_URL}/oauth/upwork/callback"

SEARCH_QUERY = """
query search($filter: MarketplaceJobFilter, $sort: [MarketplaceJobPostingSearchSortAttribute]) {
  marketplaceJobPostings(marketPlaceJobFilter: $filter, searchType: USER_JOBS_SEARCH, sortAttributes: $sort) {
    totalCount
    edges { node {
      id ciphertext title description createdDateTime
      duration engagement experienceLevel totalApplicants
      amount { rawValue }
      hourlyBudgetMin { rawValue } hourlyBudgetMax { rawValue }
      job { contractTerms { contractType } }
      skills { name prettyName }
      client {
        totalHires totalPostedJobs totalReviews totalFeedback verificationStatus
        totalSpent { rawValue }
        location { country }
      }
    } }
  }
}
"""


def configured() -> bool:
    return bool(UPWORK_CLIENT_ID and UPWORK_CLIENT_SECRET)


def authorize_url() -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": UPWORK_CLIENT_ID,
                                       "redirect_uri": REDIRECT})


async def _token_request(data: dict) -> dict:
    data |= {"client_id": UPWORK_CLIENT_ID, "client_secret": UPWORK_CLIENT_SECRET}
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.post(TOKEN_URL, data=data)
        r.raise_for_status()
        tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 86400)) - 300
    db.kv_set("upwork_token", tok)
    return tok


async def exchange_code(code: str) -> None:
    await _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT})


async def _access_token() -> str | None:
    tok = db.kv_get("upwork_token")
    if not tok:
        return None
    if time.time() >= tok["expires_at"]:
        tok = await _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    return tok["access_token"]


def connected() -> bool:
    return db.kv_get("upwork_token") is not None


async def search(expression: str, rules: dict) -> list[dict]:
    """One saved search -> newest jobs, normalized. Server-side filters narrow before we pay for anything."""
    token = await _access_token()
    if not token:
        return []
    f = rules.get("filters", {})
    flt = {"searchExpression_eq": expression, "pagination_eq": {"first": 50}, "daysPosted_eq": 1}
    if f.get("payment_verified"):
        flt["verifiedPaymentOnly_eq"] = True
    if f.get("max_proposals"):
        flt["proposalRange_eq"] = {"rangeStart": 0, "rangeEnd": int(f["max_proposals"])}
    variables = {"filter": flt, "sort": [{"field": "RECENCY"}]}
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.post(GQL, json={"query": SEARCH_QUERY, "variables": variables},
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):  # GraphQL returns 200 with errors in the body
        raise RuntimeError(f"Upwork GraphQL error: {body['errors'][:2]}")
    edges = (body.get("data") or {}).get("marketplaceJobPostings", {}).get("edges", [])
    return [normalize(e["node"], source="upwork_api") for e in edges]


def _raw(x):
    return (x or {}).get("rawValue") if isinstance(x, dict) else x


def _num(x):
    try:
        return float(_raw(x))
    except (TypeError, ValueError):
        return None


def normalize(n: dict, source: str) -> dict:
    """Common job shape used by filters, scorer, drafter, and Telegram. Also accepts webhook payloads."""
    c = n.get("client") or {}
    cipher = n.get("ciphertext") or n.get("id")
    contract = ((n.get("job") or {}).get("contractTerms") or {}).get("contractType") or n.get("job_type")
    return {
        "id": str(cipher),
        "source": source,
        "url": n.get("url") or f"https://www.upwork.com/jobs/{cipher}",
        "title": n.get("title") or "",
        "description": n.get("description") or "",
        "created": n.get("createdDateTime") or n.get("created"),
        "job_type": (contract or "").upper() or None,           # HOURLY | FIXED_PRICE
        "budget_fixed": _num(n.get("amount") or n.get("budget")),
        "hourly_min": _num(n.get("hourlyBudgetMin")),
        "hourly_max": _num(n.get("hourlyBudgetMax")),
        "experience": n.get("experienceLevel"),
        "duration": n.get("duration"),
        "proposals": n.get("totalApplicants"),
        "skills": [s.get("prettyName") or s.get("name") for s in (n.get("skills") or []) if isinstance(s, dict)]
                  or n.get("skills_list") or [],
        "client": {
            "payment_verified": str(c.get("verificationStatus", c.get("payment_verified", ""))).upper()
                                in ("VERIFIED", "TRUE", "1"),
            "hires": c.get("totalHires"),
            "jobs_posted": c.get("totalPostedJobs"),
            "reviews": c.get("totalReviews"),
            "rating": _num(c.get("totalFeedback")),
            "spent": _num(c.get("totalSpent")),
            "country": (c.get("location") or {}).get("country") or c.get("country"),
        },
    }
