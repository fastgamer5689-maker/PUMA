"""Central configuration. Secrets come from .env; tunables are plain constants."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.isdigit()}


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Environment Variables ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "gemini-3.8-flash")
OWNER_AUTH_KEY = os.getenv("OWNER_AUTH_KEY")
PROFANITY_FILE = os.getenv("PROFANITY_FILE", "profanity.txt")


# --------------------------------------------------------------------------
# Identity / secrets
# --------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN", "")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID") or 0)
# Users the anti-nuke / automod will never act against (plus the guild owner and the bot itself).
TRUSTED_IDS = _ids("TRUSTED_USER_IDS") | ({BOT_OWNER_ID} if BOT_OWNER_ID else set())
# Extra role IDs that count as "staff" for the Inside-Man protocol (in addition to permission checks).
STAFF_ROLE_IDS = _ids("STAFF_ROLE_IDS")
# Secret that lets the server owner override a permanent staff blacklist (/authorize_staff).
OWNER_AUTH_KEY = os.getenv("OWNER_AUTH_KEY", "")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5-5")

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "modbot.sqlite3"))

# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
FILTER_DIR = BASE_DIR / "filters"
HARD_BAN_FILE = FILTER_DIR / "hard_ban_terms.txt"   # instant permanent ban
SLUR_FILE = FILTER_DIR / "slur_terms.txt"           # delete + flag for human review
PROFANITY_FILE = FILTER_DIR / "profanity.txt"       # the BOT may never output these

# Log what automod WOULD do instead of banning. Strongly recommended for the first few days.
AUTOMOD_DRY_RUN = _flag("AUTOMOD_DRY_RUN", False)

# NudeNet class -> minimum confidence that counts as "explicit". Tune to taste.
NSFW_THRESHOLDS = {
    "FEMALE_GENITALIA_EXPOSED": 0.50,
    "MALE_GENITALIA_EXPOSED": 0.50,
    "ANUS_EXPOSED": 0.50,
    "FEMALE_BREAST_EXPOSED": 0.70,
    "BUTTOCKS_EXPOSED": 0.75,
}
NSFW_MAX_BYTES = 10 * 1024 * 1024

# --------------------------------------------------------------------------
# Anti-nuke
# --------------------------------------------------------------------------
NUKE_WINDOW_SECONDS = 10
NUKE_DELETE_LIMIT = 3            # channel deletions by one actor inside the window
NUKE_CREATE_LIMIT = 5            # channel creations by one actor inside the window
MASS_PING_PER_MESSAGE = 15       # mentions in a single message
MASS_PING_WINDOW_TOTAL = 30      # mentions by one actor inside the window
PARTICIPANT_MIN_EVENTS = 2       # others who did >= this many destructive events are also purged
PARTICIPANT_MIN_PINGS = 5

# --------------------------------------------------------------------------
# Moderation extras
# --------------------------------------------------------------------------
GHOST_PING_WINDOW = 300          # seconds after sending in which delete/edit counts as a ghost ping
OFFICIAL_CHANNEL_KEYWORDS = ("announcement", "rules")
EVERYONE_SPAM_LIMIT = 3          # @everyone/@here uses per minute allowed even in official channels

QUARANTINE_MINUTES = 10
JOINS_CHANNEL_NAME = "joins"
DO_NOT_TYPE_CHANNEL_NAME = "do-not-type"

# --------------------------------------------------------------------------
# Batched logging
# --------------------------------------------------------------------------
BATCH_QUIET_SECONDS = 6          # flush after this long with no new entries
BATCH_MAX_WAIT_SECONDS = 30      # ...or this long after the first entry
