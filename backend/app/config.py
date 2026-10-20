import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "backend" / "data" / "accounting.db"
MODEL = "qwen/qwen3.8-27b"
INPUT_USD_PER_MILLION = 0.0
OUTPUT_USD_PER_MILLION = 0.0


def load_env() -> None:
    for candidate in (ROOT / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


@dataclass(frozen=True)
class Settings:
    groq_api_key: str
    model: str
    extractor: str
    db_path: Path
    agentmail_api_key: str = ""
    agentmail_inbox_id: str = ""
    slack_bot_token: str = ""
    slack_default_channel: str = ""
    slack_app_id: str = ""
    slack_refresh_token: str = ""

    @property
    def google_api_key(self) -> str:
        """Backward-compatible alias used by older call sites."""
        return self.groq_api_key

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            groq_api_key=os.environ.get("GROQ_API_KEY", "").strip(),
            model=os.environ.get("GROQ_MODEL", os.environ.get("GEMINI_MODEL", MODEL)).strip() or MODEL,
            extractor=os.environ.get("EXTRACTOR", "groq").strip() or "groq",
            db_path=Path(os.environ.get("ACCOUNTING_DB", str(DEFAULT_DB))),
            agentmail_api_key=os.environ.get("AGENTMAIL_API_KEY", "").strip(),
            agentmail_inbox_id=os.environ.get("AGENTMAIL_INBOX_ID", "").strip(),
            slack_bot_token=(os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_ACCESS_TOKEN") or "").strip(),
            slack_default_channel=os.environ.get("SLACK_DEFAULT_CHANNEL", "").strip(),
            slack_app_id=os.environ.get("SLACK_APP_ID", "").strip(),
            slack_refresh_token=os.environ.get("SLACK_REFRESH_TOKEN", "").strip(),
        )
