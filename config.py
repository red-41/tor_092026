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
MAX_DETAIL_PAGES = int(os.environ.get("MAX_DETAIL_PAGES", "40"))    # production pages per source (tiers 0 and 1)
DETAIL_CAP = {0: MAX_DETAIL_PAGES, 1: MAX_DETAIL_PAGES, 2: 15, 3: 15}  # tiers 2-3: most dates are on the listing
DETAIL_REFRESH_DAYS = 30        # a production page already read is re-read at most monthly...
DETAIL_SOON_DAYS = 28           # ...or weekly-ish once its next show is less than 4 weeks away (cancellations, times)
USE_BATCH = os.environ.get("USE_BATCH", "1") != "0"   # production pages go through the half-price Batch API
JOB_MINUTES = float(os.environ.get("JOB_MINUTES", "340"))  # stop waiting for the batch before GitHub's 6-hour limit
LISTING_BATCH_MINUTES = 150     # round 1: wait for the schedule-page batch until this many minutes into the job
DETAIL_RESERVE_MINUTES = 60     # round 2: stop opening production pages this long before the end, so their batch can finish
PAGE_TEXT_LIMIT = int(os.environ.get("PAGE_TEXT_LIMIT", "60000"))   # characters sent to the model per page
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2.0"))       # politeness delay between page loads
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))
MAX_SOURCE_MINUTES = float(os.environ.get("MAX_SOURCE_MINUTES", "25"))  # per source, so one huge site cannot eat the run
CHALLENGE_WAIT_S = 20                                                # how long to wait for a "checking your browser" page to clear

# How long until a source is due again, by priority (days).
RECHECK_DAYS = {0: 7, 1: 14, 2: 30, 3: 30}   # tier 0 is the weekly core
FESTIVAL_WAIT_DAYS = 30                      # a festival with no programme out yet is looked at monthly

# Time zone of a performance comes from the country it happens in (touring companies play abroad).
TZ_BY_COUNTRY = {
    "Albania": "Europe/Tirane", "Armenia": "Asia/Yerevan", "Austria": "Europe/Vienna", "Belgium": "Europe/Brussels",
    "Bosnia and Herzegovina": "Europe/Sarajevo", "Bulgaria": "Europe/Sofia", "Croatia": "Europe/Zagreb", "Cyprus": "Asia/Nicosia",
    "Czech Republic": "Europe/Prague", "Czechia": "Europe/Prague", "Denmark": "Europe/Copenhagen", "Estonia": "Europe/Tallinn",
    "Finland": "Europe/Helsinki", "France": "Europe/Paris", "Georgia": "Asia/Tbilisi", "Germany": "Europe/Berlin",
    "Greece": "Europe/Athens", "Hungary": "Europe/Budapest", "Iceland": "Atlantic/Reykjavik", "Ireland": "Europe/Dublin",
    "Italy": "Europe/Rome", "Latvia": "Europe/Riga", "Liechtenstein": "Europe/Vaduz", "Lithuania": "Europe/Vilnius",
    "Luxembourg": "Europe/Luxembourg", "Malta": "Europe/Malta", "Moldova": "Europe/Chisinau", "Monaco": "Europe/Monaco",
    "Montenegro": "Europe/Podgorica", "Netherlands": "Europe/Amsterdam", "North Macedonia": "Europe/Skopje", "Norway": "Europe/Oslo",
    "Poland": "Europe/Warsaw", "Portugal": "Europe/Lisbon", "Romania": "Europe/Bucharest", "Russia": "Europe/Moscow",
    "Serbia": "Europe/Belgrade", "Slovakia": "Europe/Bratislava", "Slovenia": "Europe/Ljubljana", "Spain": "Europe/Madrid",
    "Sweden": "Europe/Stockholm", "Switzerland": "Europe/Zurich", "Turkey": "Europe/Istanbul", "Ukraine": "Europe/Kyiv",
    "United Kingdom": "Europe/London", "UK": "Europe/London", "Israel": "Asia/Jerusalem", "United States": "America/New_York",
    "USA": "America/New_York", "Canada": "America/Toronto", "Australia": "Australia/Sydney", "Japan": "Asia/Tokyo",
}

GENRE_TAGS = ["Classical", "Neoclassical", "Contemporary"]
EXTRA_TAGS = ["Premiere", "Gala", "Festival", "Family", "Hip hop", "Tap", "Physical theatre", "Dance theatre", "Immersive"]
