"""Gate 1: hard rules in plain code. Free, instant, and every rejection records which rule killed it."""
import re


def _has_any(text: str, terms: list[str]) -> str | None:
    for t in terms or []:
        if re.search(r"\b" + re.escape(t.lower()) + r"\b", text):
            return t
    return None


def check(job: dict, rules: dict) -> str | None:
    """Return None if the job passes, else the reason it failed."""
    f = rules.get("filters", {})
    c = job.get("client", {})
    text = f"{job.get('title', '')} {job.get('description', '')} {' '.join(job.get('skills') or [])}".lower()

    if f.get("payment_verified") and not c.get("payment_verified"):
        return "payment not verified"

    if job.get("job_type") == "HOURLY":
        top = job.get("hourly_max") or job.get("hourly_min")
        if top is not None and f.get("min_hourly") and top < f["min_hourly"]:
            return f"hourly max ${top:.0f} < ${f['min_hourly']}"
    elif job.get("budget_fixed") is not None and f.get("min_fixed_budget"):
        if 0 < job["budget_fixed"] < f["min_fixed_budget"]:
            return f"budget ${job['budget_fixed']:.0f} < ${f['min_fixed_budget']}"

    if f.get("max_proposals") and (job.get("proposals") or 0) > f["max_proposals"]:
        return f"{job['proposals']} proposals > {f['max_proposals']}"

    min_hires, min_spent = f.get("min_client_hires", 0), f.get("min_client_spent", 0)
    if min_hires or min_spent:
        if (c.get("hires") or 0) < min_hires and (c.get("spent") or 0) < min_spent:
            return "client has no hire/spend history"

    if f.get("min_client_rating") and c.get("rating") and c["rating"] < f["min_client_rating"]:
        return f"client rating {c['rating']:.1f} < {f['min_client_rating']}"

    if c.get("country") and c["country"] in (f.get("exclude_countries") or []):
        return f"client country {c['country']}"

    if (bad := _has_any(text, f.get("exclude_terms"))):
        return f"excluded term '{bad}'"

    if f.get("include_terms") and not _has_any(text, f["include_terms"]):
        return "no include term"

    return None
