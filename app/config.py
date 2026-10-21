"""Settings from environment + editable rules from config/rules.yaml (copied to the disk on first boot)."""
import hashlib
import json
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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip("\'\"")
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip().strip("\'\"")  # only for keys not scoped to one workspace
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip().strip("\'\"")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "").strip().strip("\'\"")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip().strip("\'\"")
UPWORK_CLIENT_ID = os.environ.get("UPWORK_CLIENT_ID", "").strip().strip("\'\"")
UPWORK_CLIENT_SECRET = os.environ.get("UPWORK_CLIENT_SECRET", "").strip().strip("\'\"")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip().strip("\'\"")
INGEST_WEBHOOK_SECRET = os.environ.get("INGEST_WEBHOOK_SECRET", "").strip().strip("\'\"")
# Render sets RENDER_EXTERNAL_URL to the service's real address; it wins over a hand-typed value.
PUBLIC_BASE_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_BASE_URL")
                   or "http://localhost:8000").rstrip("/")

SCORER_MODEL = os.environ.get("SCORER_MODEL", "claude-haiku-5-5")
DRAFTER_MODEL = os.environ.get("DRAFTER_MODEL", "claude-sonnet-5-5")


SEED_MANIFEST = DATA_DIR / ".seeded.json"
TEMPLATE_MARKERS = ("<!-- TEMPLATE", "# PLACEHOLDERS")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


OLD_PLACEHOLDER_SEARCHES = [["automation OR integration", "AI agent OR LLM OR Claude"]]


def _untouched(disk: Path, seeded: dict, rel: str) -> bool:
    """A disk file is safe to replace if it is exactly what we last seeded, or still a template."""
    if seeded.get(rel) == _sha(disk):
        return True
    if rel == "rules.yaml":  # a re-saved copy of the first-boot placeholder rules still counts as a template
        try:
            if (yaml.safe_load(disk.read_text()) or {}).get("searches") in OLD_PLACEHOLDER_SEARCHES:
                return True
        except yaml.YAMLError:
            return False
    return rel not in seeded and any(m in disk.read_text(errors="ignore") for m in TEMPLATE_MARKERS)


def sync_from_repo() -> list[str]:
    """Push repo rules + knowledge to the disk on every boot, never overwriting a file you edited
    in the dashboard. Files removed from the repo are removed from the disk only if untouched."""
    seeded = json.loads(SEED_MANIFEST.read_text()) if SEED_MANIFEST.exists() else {}
    pairs = {"rules.yaml": (ROOT / "config" / "rules.yaml", RULES_PATH)}
    for src in (ROOT / "knowledge").rglob("*.md"):
        rel = "knowledge/" + str(src.relative_to(ROOT / "knowledge"))
        pairs[rel] = (src, KNOWLEDGE_DIR / src.relative_to(ROOT / "knowledge"))
    changed = []
    for rel, (src, dst) in pairs.items():
        if dst.exists() and (_sha(dst) == _sha(src) or not _untouched(dst, seeded, rel)):
            if _sha(dst) == _sha(src):
                seeded[rel] = _sha(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        seeded[rel] = _sha(dst)
        changed.append(rel)
    for rel in [r for r in seeded if r not in pairs]:
        dst = DATA_DIR / rel if rel.startswith("knowledge/") else RULES_PATH
        if dst.exists() and _untouched(dst, seeded, rel):
            dst.unlink()
            changed.append(f"removed {rel}")
        seeded.pop(rel)
    # template files seeded before this sync existed and since deleted from the repo
    for dst in KNOWLEDGE_DIR.rglob("*.md") if KNOWLEDGE_DIR.exists() else []:
        rel = "knowledge/" + str(dst.relative_to(KNOWLEDGE_DIR))
        if rel not in pairs and rel not in seeded and _untouched(dst, seeded, rel):
            dst.unlink()
            changed.append(f"removed {rel}")
    SEED_MANIFEST.write_text(json.dumps(seeded, indent=1))
    return changed


SYNCED = sync_from_repo()


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
