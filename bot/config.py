"""
Central place for all environment-driven configuration.
Nothing secret is hard-coded here — everything comes from the environment
so the same code runs locally, in Docker, or on a GCE VM without edits.
"""
import os

from dotenv import load_dotenv

# Picks up a .env file in the working directory if present (local/dev use).
# On Fly.io / systemd, real env vars or secrets are already set and this is a no-op.
load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your shell, .env file, or systemd unit."
        )
    return val


# --- Telegram ---
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# --- LLM providers ---
# Primary: direct Gemini API. Fallback: AIPipe (OpenAI-compatible) when the
# Gemini key hits a quota/rate error. This mirrors the pattern already used
# elsewhere in the course work.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# "gemini-flash-latest" is Google's maintained alias for their current
# recommended Flash model — avoids hardcoding a version number that Google
# later retires for new API keys (as happened with gemini-2.5-flash).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

AIPIPE_API_KEY = os.environ.get("AIPIPE_API_KEY", "")
AIPIPE_BASE_URL = os.environ.get("AIPIPE_BASE_URL", "https://aipipe.org/openai/v1")
AIPIPE_MODEL = os.environ.get("AIPIPE_MODEL", "gemini-flash-latest")

if not GEMINI_API_KEY and not AIPIPE_API_KEY:
    raise RuntimeError(
        "Set at least one of GEMINI_API_KEY or AIPIPE_API_KEY so the agent has an LLM to call."
    )

# --- GCS logging ---
# Reuses the same bucket/project pattern from the Q3/Q4 GCS tasks.
GCS_BUCKET = _require("GCS_LOG_BUCKET")
GCS_LOG_PREFIX = os.environ.get("GCS_LOG_PREFIX", "logs")

# --- Agent behaviour ---
MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "8"))
TOOL_TIMEOUT_SECONDS = int(os.environ.get("TOOL_TIMEOUT_SECONDS", "20"))
CONVERSATION_TTL_SECONDS = int(os.environ.get("CONVERSATION_TTL_SECONDS", "1800"))
POLL_TIMEOUT_SECONDS = int(os.environ.get("POLL_TIMEOUT_SECONDS", "30"))
