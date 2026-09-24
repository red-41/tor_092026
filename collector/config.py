"""Settings, read from environment variables (GitHub secrets in production)."""
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Model used to read pages. Override with the MODEL secret or variable.
MODEL = os.environ.get("MODEL", "claude-sonnet-5")

# Pages are opened as a normal, current Chrome (empty = match the installed browser version).
USER_AGENT = os.environ.get("USER_AGENT", "")
# robots.txt rules are checked for this name, and robots.txt itself is fetched with BOT_UA.
BOT_NAME = "SaffittBot"
BOT_UA = "Mozilla/5.0 (compatible; SaffittBot/1.0; +https://saffitt.com)"

MAX_LISTING_PAGES = int(os.environ.get("MAX_LISTING_PAGES", "8"))   # pagination pages per source
MAX_DETAIL_PAGES = int(os.environ.get("MAX_DETAIL_PAGES", "40"))    # production pages per source
PAGE_TEXT_LIMIT = int(os.environ.get("PAGE_TEXT_LIMIT", "60000"))   # characters sent to the model per page
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2.0"))       # politeness delay between page loads
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))
MAX_SOURCE_MINUTES = float(os.environ.get("MAX_SOURCE_MINUTES", "25"))  # per source, so one huge site cannot eat the run
CHALLENGE_WAIT_S = 20                                                # how long to wait for a "checking your browser" page to clear

# How long until a source is due again, by priority (days).
RECHECK_DAYS = {1: 7, 2: 14, 3: 30}

GENRE_TAGS = ["Classical", "Neoclassical", "Contemporary"]
EXTRA_TAGS = ["Premiere", "Gala", "Festival", "Family", "Flamenco", "Hip hop", "Tap", "Indian dance",
              "Folk dance", "Physical theatre", "Dance theatre", "Immersive"]
