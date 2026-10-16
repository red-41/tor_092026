"""Settings, read from environment variables (GitHub secrets in production)."""
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Model used to read pages. Override with the MODEL repository variable.
MODEL = os.environ.get("MODEL") or "claude-haiku-5-5"
# Second look: a page whose first reading looks doubtful (answer cut off, the model says it is unsure, or far fewer
# shows than last time) is read again by this model. Empty = no second look. Sonnet 5, not 5.5: Sonnet 5.5 refuses the
# forced answer form (tool_choice "tool") the collector uses; the price is the same.
ESCALATE_MODEL = os.environ.get("ESCALATE_MODEL", "claude-sonnet-5")
# Models that refuse a forced tool call (they answer with tool_choice "auto"; the prompt still says to call the tool).
# Source: https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use (checked 16 Oct 2026)
NO_FORCED_TOOL = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"}
ESCALATE_MAX_SHARE = float(os.environ.get("ESCALATE_MAX_SHARE", "0.15"))   # at most this share of a run's pages
ESCALATE_DROP = 0.5             # "far fewer": under half the shows of the last reading of the same page...
ESCALATE_DROP_MIN = 3           # ...when that reading had at least this many
# Readings by these models are reused for an unchanged page (a stronger model's reading is at least as good).
CACHE_MODELS = {MODEL, ESCALATE_MODEL, "claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5-5"} - {""}

# Spending cap (USD), shared by all jobs of a run and the duplicate check. The month is the UTC calendar month.
MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD") or 100)
RUN_BUDGET_USD = float(os.environ.get("RUN_BUDGET_USD") or 25)
BUDGET_BLOCK_USD = 0.25         # reserved from the ledger at a time
EST_OUTPUT_TOKENS = 1500        # answer length assumed before an answer comes back (corrected afterwards)
# USD per million tokens: (input, output, cache write 5m, cache read). Batch API: half of these.
# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 16 Oct 2026)
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.0, 10.0, 2.50, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 2.50, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
}
LONG_PROMPT_TOKENS = 100_000    # Haiku 5.5 costs 5x for prompts above this...
LONG_PROMPT_FACTOR = {"claude-haiku-5-5": 5.0}
MAX_PROMPT_TOKENS = 85_000      # ...so a page's request is trimmed to stay under it (estimated generously)

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
DETAIL_NODATE_DAYS = 14         # a production page showing no upcoming date is looked at again after two weeks
ANNOUNCE_MAX_WAIT_DAYS = 60     # a site with nothing on yet waits for its announced date, but never longer than this
# --compare: reference readings (an unchanged page's earlier reading by these models is reused for free)
COMPARE_REF_MODELS = {"claude-sonnet-5", "claude-sonnet-5-5"}
COMPARE_FRESH_MAX = int(os.environ.get("COMPARE_FRESH_MAX", "60"))   # fresh reference readings allowed per test
COMPARE_DETAIL_CAP = 6          # production pages per source in the test
USE_BATCH = os.environ.get("USE_BATCH", "1") != "0"   # production pages go through the half-price Batch API
JOB_MINUTES = float(os.environ.get("JOB_MINUTES", "320"))  # everything stops by then: well inside GitHub's 355-minute job limit
LISTING_BATCH_MINUTES = 120     # round 1: wait for the schedule-page batch until this many minutes into the job
DETAIL_RESERVE_MINUTES = 45     # round 2: no page is opened in the last 45 minutes, so the last answers can come back
BATCH_SEND_AT = 40              # production pages are sent to Claude in bundles of about this size...
BATCH_SEND_EVERY_S = 600        # ...or every 10 minutes, whichever comes first, so sites are saved as they finish
PAGE_HARD_LIMIT_S = 300         # a page still loading after 5 minutes has frozen the browser: it is restarted
PAGE_TEXT_LIMIT = int(os.environ.get("PAGE_TEXT_LIMIT", "60000"))   # characters sent to the model per page
FEED_TEXT_LIMIT = 8000          # characters of each background data feed sent along (up to 3 per page)
SITEMAP_LIMIT = 200             # event/production addresses from a site's sitemap sent with its first schedule page
# Tiers 0 and 1 get the thorough treatment: all calendar months to the season's end, sitemap, date hunt.
CORE_TIERS = (0, 1)
MAX_LISTING_PAGES_CORE = 14     # schedule pages per source for tiers 0-1 (a season is up to 12 months)
MONTHS_AHEAD = 12
MONTH_LOOKAHEAD = 3              # a calendar by month is read until this many months past the last month with shows
HUNT_PAGES = 10                 # extra pages per source to find the individual dates of a run (tiers 0-1)
MAX_EXTRA_START_PAGES = 6       # other dance categories / calendars remembered per source
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
