"""
platform/config.py
==================
Loads platform-level settings from the .env file.

This is the ONLY place .env is read. All other modules import from here.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_PROJECT_ROOT / ".playwright-browsers")


class PlatformConfig:
    """
    Central configuration object.
    All values come from environment variables (.env file).
    """

    # ── AI API Keys ──────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")


    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ADMIN_CHAT_ID: str = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")


    # The tenant whose data this config object addresses. Set per request by
    # dashboard.get_workflow(); never a global default.
    ACTIVE_GROUP_ID: str = ""

    # ── Serper Search ───────────────────────────────────────────────────────────────────
    SEARCH_API_KEY: str = os.getenv("SEARCH_API_KEY", "")
    SEARCH_API_PROVIDER: str = os.getenv("SEARCH_API_PROVIDER", "serper")

    # ── Database ────────────────────────────────────────────────────────────────────────
    # Postgres connection string. services/storage/db.py normalises the driver
    # prefix and drops a pooled endpoint before use.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # ── Runtime ───────────────────────────────────────────────────────────────────────
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "dev")
    FLASK_SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY", "")
    PROJECT_ROOT: Path = _PROJECT_ROOT
    GROUPS_DIR: Path = _PROJECT_ROOT / "groups"

    @classmethod
    def is_telegram_configured(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN and cls.TELEGRAM_CHAT_ID
                    and "xxxxxxxxxx" not in cls.TELEGRAM_CHAT_ID)

    @classmethod
    def is_search_configured(cls) -> bool:
        return bool(cls.SEARCH_API_KEY)


    @classmethod
    def validate(cls) -> list[str]:
        """Returns a list of missing/invalid config items."""
        issues = []
        if not cls.is_telegram_configured():
            issues.append("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        if not cls.is_search_configured():
            issues.append("SEARCH_API_KEY not set in .env")
        return issues


# Singleton instance — import this everywhere
config = PlatformConfig()

