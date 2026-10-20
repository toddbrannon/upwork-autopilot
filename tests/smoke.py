"""Offline smoke test: normalize -> filters -> db -> dashboard/ingest endpoints, no network or API keys."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DASHBOARD_PASSWORD"] = "pw"
os.environ["INGEST_WEBHOOK_SECRET"] = "s3cret"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, filters, upwork  # noqa: E402
from app.config import load_rules  # noqa: E402
from app.main import app  # noqa: E402

NODE = {
    "id": "1", "ciphertext": "~01abc", "title": "Build an AI agent to automate lead intake",
    "description": "Need a Python automation with the Claude API and Zapier.", "createdDateTime": "2026-10-20T15:00:00Z",
    "totalApplicants": 4, "experienceLevel": "EXPERT", "job": {"contractTerms": {"contractType": "HOURLY"}},
    "hourlyBudgetMin": {"rawValue": "50"}, "hourlyBudgetMax": {"rawValue": "90"},
    "skills": [{"name": "python", "prettyName": "Python"}],
    "client": {"totalHires": 3, "totalSpent": {"rawValue": "5200"}, "totalFeedback": 4.9,
               "verificationStatus": "VERIFIED", "location": {"country": "United States"}},
}


def main():
    rules = load_rules()
    job = upwork.normalize(NODE, "upwork_api")
    assert job["url"] == "https://www.upwork.com/jobs/~01abc" and job["hourly_max"] == 90
    assert filters.check(job, rules) is None, filters.check(job, rules)

    cheap = upwork.normalize({**NODE, "hourlyBudgetMax": {"rawValue": "20"}, "hourlyBudgetMin": None}, "x")
    assert "hourly" in filters.check(cheap, rules)
    unverified = upwork.normalize({**NODE, "client": {**NODE["client"], "verificationStatus": "UNVERIFIED"}}, "x")
    assert filters.check(unverified, rules) == "payment not verified"
    spam = upwork.normalize({**NODE, "description": "unpaid test first"}, "x")
    assert "unpaid test" in filters.check(spam, rules)

    with TestClient(app) as c:
        assert c.get("/health").json()["ok"]
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("u", "pw")).status_code == 200
        assert c.post("/ingest/wrong", json=NODE).status_code == 404
        r = c.post("/ingest/s3cret", json=[NODE, NODE])
        assert r.json()["new"] == 1, r.json()          # deduped
        assert db.mark_seen(job["id"], "x") is False
        assert c.get("/knowledge/../../etc/passwd.md", auth=("u", "pw")).status_code in (400, 404)
        assert c.get("/knowledge/profile.md", auth=("u", "pw")).status_code == 200
    print("smoke OK")


if __name__ == "__main__":
    main()
