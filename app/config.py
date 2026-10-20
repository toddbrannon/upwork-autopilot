"""Settings from environment + editable rules from config/rules.yaml (copied to the disk on first boot)."""
import os
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "autopilot.sqlite3"
RULES_PATH = DATA_DIR / "rules.yaml"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
UPWORK_CLIENT_ID = os.environ.get("UPWORK_CLIENT_ID", "")
UPWORK_CLIENT_SECRET = os.environ.get("UPWORK_CLIENT_SECRET", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
INGEST_WEBHOOK_SECRET = os.environ.get("INGEST_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

SCORER_MODEL = os.environ.get("SCORER_MODEL", "claude-haiku-5-5")
DRAFTER_MODEL = os.environ.get("DRAFTER_MODEL", "claude-sonnet-5-5")


def _seed_disk() -> None:
    """First boot: copy the repo's starter rules and knowledge base onto the persistent disk.
    After that, the disk copies are the live ones (edited from the dashboard or by you)."""
    if not RULES_PATH.exists():
        shutil.copy(ROOT / "config" / "rules.yaml", RULES_PATH)
    if not KNOWLEDGE_DIR.exists():
        shutil.copytree(ROOT / "knowledge", KNOWLEDGE_DIR)


_seed_disk()


def load_rules() -> dict:
    with open(RULES_PATH) as f:
        return yaml.safe_load(f) or {}


def save_rules(rules: dict) -> None:
    with open(RULES_PATH, "w") as f:
        yaml.safe_dump(rules, f, sort_keys=False)


def load_knowledge() -> str:
    """Concatenate every markdown file in the knowledge folder into one cacheable block."""
    parts = []
    for p in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        rel = p.relative_to(KNOWLEDGE_DIR)
        parts.append(f"<file path=\"{rel}\">\n{p.read_text().strip()}\n</file>")
    return "\n\n".join(parts)
