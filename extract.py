"""Ask Claude to read one page and return structured dance performances."""
import datetime as dt
import json
import re
import time

import anthropic

import config
import lenses

SOCIAL = re.compile(r"(facebook|instagram|twitter|x\.com|youtube|youtu\.be|vimeo|tiktok|linkedin|spotify|pinterest|"
                    r"google\.|apple\.com|mailto:|whatsapp|t\.me|flickr|soundcloud)", re.I)

TOOL = {
    "name": "record_page",
    "description": "Record what this page says about upcoming dance and ballet performances.",
    "input_schema": {
        "type": "object",
        "properties": {
            "page_status": {
                "type": "string",
                "enum": ["has_performances", "listing_without_dates", "no_dance_programme",
                         "not_published_yet", "not_a_schedule"],
                "description": "has_performances: dated dance performances found. listing_without_dates: dance productions "
                               "listed but dates/times are on their own pages. no_dance_programme: schedule has no dance. "
                               "not_published_yet: programme announced for later. not_a_schedule: wrong page.",
            },
            "productions": {
                "type": "array",
                "description": "Dance productions on this page with their dates. For a mixed bill (several pieces in one evening) "
                               "one entry per piece, each with the same performances list, plus program and program_position.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "English title of the piece. Use the venue's English title if it has one; translate plain-word titles; keep proper names."},
                        "original_title": {"type": ["string", "null"], "description": "Title in the venue's language if different from title."},
                        "work": {"type": ["string", "null"], "description": "The work. For classic story ballets use one English story name (Swan Lake, Giselle, The Nutcracker, Romeo and Juliet, The Sleeping Beauty, Don Quixote, La Bayadère, Coppélia, Manon, Onegin, Cinderella, Carmen...). Otherwise the piece title. Null for an untitled new creation."},
                        "work_is_story": {"type": "boolean", "description": "True when work is a classic story shared by many productions."},
                        "choreographer": {"type": ["string", "null"], "description": "Choreographer of this piece (one name, or a duo as billed). Null only if the page gives none."},
                        "company": {"type": ["string", "null"], "description": "Company dancing it."},
                        "tags": {"type": "array", "items": {"type": "string"},
                                 "description": "At least one of Classical, Neoclassical, Contemporary, plus any of: " + ", ".join(config.EXTRA_TAGS)},
                        "program": {"type": ["string", "null"], "description": "Name of the evening when several pieces share one ticket (mixed bill). If it has no name, the choreographers' names joined with ' / '. Null for a single-work evening."},
                        "program_position": {"type": ["integer", "null"], "description": "Running order of this piece in the evening, 1-based, as listed."},
                        "image_url": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"], "description": "One or two sentences in your own words, in English. Never copy the site's text."},
                        "detail_url": {"type": ["string", "null"], "description": "Page of this production on the site, if linked."},
                        "runs": {
                            "type": "array",
                            "description": "Only for a venue where this production's individual dates are NOT all listed here "
                                           "(only a date range, or fewer dates than announced). One entry per venue.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "venue": {"type": ["string", "null"]},
                                    "city": {"type": ["string", "null"]},
                                    "country": {"type": ["string", "null"]},
                                    "start": {"type": ["string", "null"], "description": "First date YYYY-MM-DD."},
                                    "end": {"type": ["string", "null"], "description": "Last date YYYY-MM-DD."},
                                    "expected_performances": {"type": ["integer", "null"], "description": "How many performances the page says there are, if it says."},
                                    "dates_page_url": {"type": ["string", "null"], "description": "Link on this page to where the individual dates are listed: a dates, sessions, calendar or booking page of this venue or company. Never a third-party ticket seller (Ticketmaster, Eventim, See Tickets, Fnac and the like)."},
                                    "venue_event_url": {"type": ["string", "null"], "description": "For a touring stop: link to the host venue's own page for this production. Never a third-party ticket seller."},
                                },
                            },
                        },
                        "performances": {
                            "type": "array",
                            "description": "Every upcoming date and time of this production found on this page.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string", "description": "YYYY-MM-DD. Work out missing years from the season and check the weekday."},
                                    "time": {"type": ["string", "null"], "description": "Start time HH:MM local, or null if not published."},
                                    "venue": {"type": ["string", "null"], "description": "Building. Reuse an exact name from the known venues list when it is the same place."},
                                    "city": {"type": ["string", "null"]},
                                    "country": {"type": ["string", "null"]},
                                    "ticket_url": {"type": ["string", "null"], "description": "Booking link for this date if there is one."},
                                    "cancelled": {"type": "boolean"},
                                    "timezone": {"type": ["string", "null"], "description": "IANA time zone of this venue's location, e.g. Europe/Paris, America/Los_Angeles."},
                                    "date_basis": {"type": "string", "enum": ["listed", "pattern"], "description": "listed: this date is written on the page (or in its data). pattern: worked out from a weekly pattern the page states."},
                                },
                                "required": ["date"],
                            },
                        },
                    },
                    "required": ["title", "tags", "performances"],
                },
            },
            "detail_links": {"type": "array", "items": {"type": "string"},
                             "description": "URLs of single dance productions on this site whose individual dates or times are NOT on this page and must be opened. Include dance productions from the sitemap list if given."},
            "listing_links": {"type": "array", "items": {"type": "string"},
                              "description": "Up to 4 URLs on this site of pages that list several dance productions and are not this page: the ballet or dance "
                                             "programme, every other dance-related category or filter (ballet, dance, contemporary, guest performances, "
                                             "family dance, a dance festival series), the season overview or the full calendar. Never single productions."},
            "expected_performances": {"type": ["integer", "null"], "description": "Production page only: total number of performances the page announces (e.g. '12 performances'), if it says."},
            "next_page_url": {"type": ["string", "null"], "description": "Next page of this listing (pagination or next month), if any."},
            "schedule_url_guess": {"type": ["string", "null"], "description": "If this is not the schedule, or it shows no dance: the link most likely to be the dance/ballet programme, dance category or full calendar."},
            "notes": {"type": ["string", "null"], "description": "Anything the editor should know (e.g. programme announced on a date)."},
        },
        "required": ["page_status", "productions", "detail_links"],
    },
}

SYSTEM = """You extract upcoming dance and ballet performances from theatre and festival websites for Saffitt, a dance calendar.
Rules:
- Only staged dance and ballet performances open to the public. Leave out workshops, classes, open rehearsals, talks, guided tours, cinema screenings, exhibitions, amateur or school showcases, and opera/concerts without dance.
- Children's and family dance and ballet are wanted when the public can buy tickets, also on weekday mornings; tag them Family. Leave out only performances reserved for school groups ("school performance", "séance scolaire", "Schulvorstellung", "voorstelling voor scholen", booked by schools only), and children's plays, musicals, VR or digital experiences that are not dance (a children's piece stays only if it is ballet or dance).
- Leave out folk, traditional and ethnic dance: folk ensembles, national folk dance companies, and shows built on folk dance (Hungarian, Transylvanian, Carpathian, Balkan, Irish, Indian classical, flamenco, tango shows and similar), also when a dance theatre presents them. This means flamenco performances themselves (flamenco shows, tablao, flamenco recitals). Established Spanish dance companies (for example Ballet Nacional de España, Ballet Español de la Comunidad de Madrid) stay: their work counts as flamenco-inspired ballet. When a show is on the line between flamenco and flamenco-inspired ballet or contemporary dance, keep it. Flamenco-inspired ballets and contemporary choreographies always stay in, and so does every work by Marcos Morau / La Veronal (for example Afanador), whatever company dances it. Shows by flamenco artists (for example Rocío Molina, Israel Galván) are left out, except when this site is a tier 0 or tier 1 venue or festival and it presents the show as contemporary dance, not as flamenco.
- Only performances on or after today's date.
- Give every date and time of a production in its performances list. A run of 12 dates has 12 entries.
- List only the date and time pairs the page actually shows. Never pair every date of a run with every time mentioned: if the page lists times per day (matinees only on some days, a day without shows), follow it day by day.
- Mixed bill (several pieces in one evening): one production entry per piece, each with the evening's dates, plus program and program_position.
- Titles in English (use the site's English version if there is one), with original_title for the native title when different.
- Genre tags: always at least one of Classical, Neoclassical, Contemporary.
- If the time is not published, set time to null. Never guess a time.
- description: your own words, 1 to 2 sentences, never copied.
- Use absolute URLs. Prefer the booking link for that exact date as ticket_url, else the production page.
- If dates or times are only on production pages, list those pages in detail_links (dance productions only).
- Never turn a date range into individual dates. List only dates that are written on the page or in its data. If a venue's run shows only a range, or fewer dates than announced, describe it in the production's runs (start, end, how many performances are announced, where the individual dates may be). Exception: if the page states a weekly pattern ("Thu and Fri 20:30, Sat 16:30 and 20:30"), work out the dates from it and mark them date_basis "pattern".
- Touring: every date belongs to the venue, city and country shown with that date, also in small print ("on tour", "Gastspiel", "en tournée", "gira", a city name or a flag). On a venue's own schedule, a date marked with another venue or city happens there, not at the house. Never fill in the house or the company's home city for a date that names another place; leave venue null when no place is given.
- Give each performance the IANA time zone of its venue's location.
- Times are local wall-clock times at the venue. Background data often stores times in UTC or with an offset ("2026-10-01T17:30:00Z", "+02:00"): convert them to the venue's local time. When the page text shows a time, that time wins.
- List each performance once. The same show on the same date must not appear twice at different times (for example a UTC copy one or two hours off) or once with and once without its venue.
- title is the name of the work or programme, never the company's name alone and never a tour label ("International Tour", "On tour", "Gastspiel"). If the page gives only the company and a tour label, use the work's name from the page; if there is none, leave the item out.
- Background data and calendar files, when given, are part of the page: read the dates in them.
- dates_page_url and venue_event_url must be the venue's or company's own pages, never a third-party ticket seller.
- On a homepage or a general page, put the site's dance/ballet programme, dance category or calendar pages in listing_links, and the best one in schedule_url_guess.
- If the page shows no dance but the site has a dance programme, dance filter or full calendar, give it in schedule_url_guess.
Always answer by calling record_page."""


class OutOfCredit(Exception):
    """The Anthropic account has no credit left."""


def _credit_error(e: Exception) -> bool:
    text = str(e).lower()
    return "credit balance" in text or "billing" in text or "insufficient" in text and "credit" in text


class Extractor:
    def __init__(self, model: str | None = None):
        # generous retries: new Anthropic accounts have low per-minute limits; the SDK waits as told by the API
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=10, timeout=600)
        self.model = model or config.MODEL
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.cache_hits = 0
        self.out_of_credit = False      # set once the account runs dry; nothing more is sent after that

    def check(self) -> None:
        """Fail fast with a clear message if the key or the model does not work."""
        try:
            self.client.messages.create(model=self.model, max_tokens=5,
                                        messages=[{"role": "user", "content": "Reply with OK."}])
        except anthropic.AuthenticationError:
            raise SystemExit("ANTHROPIC_API_KEY was refused. Check the GitHub secret (Settings > Secrets > Actions).")
        except anthropic.PermissionDeniedError as e:
            raise SystemExit(f"Anthropic refused the request (credit or permissions?): {e}")
        except anthropic.NotFoundError:
            raise SystemExit(f"Model '{self.model}' is not available to this key. Set the MODEL variable to one that is.")
        except anthropic.APIStatusError as e:
            if _credit_error(e):
                raise SystemExit("The Anthropic credit is used up. Add credit in the Console (Settings > Billing) and run again.")
            raise

    def request_params(self, snap, source: dict, venues: list[dict], role: str) -> dict:
        """The Claude request for one page (used live or inside a batch)."""
        today = dt.date.today().isoformat()
        venue_names = sorted({v["name"] for v in venues if v.get("name")})[:300]
        site = snap.final_url.split("/")[2] if "//" in snap.final_url else ""
        same_site = [(t, h) for t, h in snap.links if site and site.split(".")[-2] in h][:300]
        mine = set(same_site)
        other_sites = [(t, h) for t, h in snap.links if (t, h) not in mine and not lenses.is_ticket_seller(h)
                       and not SOCIAL.search(h)][:80]
        events = json.dumps(snap.events, ensure_ascii=False)[:20000] if snap.events else ""
        user = (
            f"Today: {today}\n"
            f"Source: {source.get('name')} ({source.get('kind')}, tier {source.get('priority')}), {source.get('city')}, {source.get('country')}. "
            f"Time zone {source.get('timezone')}.\n"
            f"This page is a {role}.\nURL: {snap.final_url}\nPage title: {snap.title}\n"
            f"og:image: {snap.og_image}\n\n"
            f"Known venues in this country (reuse exact names): {'; '.join(venue_names) or 'none'}\n\n"
            + (f"Structured event data found in the page (schema.org and calendar files):\n{events}\n\n" if events else "")
            + ("Data this page loaded in the background (may hold the individual dates):\n"
               + "\n".join(f"{f['url']}\n{f['text']}" for f in (getattr(snap, 'feeds', None) or [])) + "\n\n"
               if getattr(snap, "feeds", None) else "")
            + ("Event and production pages listed in this site's sitemap (addresses only). Put the dance productions "
               "among them whose dates are not on this page into detail_links:\n"
               + "\n".join(getattr(snap, "sitemap", None) or []) + "\n\n" if getattr(snap, "sitemap", None) else "")
            + "Links on the page (text | url):\n" + "\n".join(f"{t} | {h}" for t, h in same_site) + "\n\n"
            + ("Links to other sites, e.g. host venues of a tour (text | url):\n"
               + "\n".join(f"{t} | {h}" for t, h in other_sites) + "\n\n" if other_sites else "")
            + "Page text:\n" + snap.text[: config.PAGE_TEXT_LIMIT]
        )
        return dict(
            model=self.model, max_tokens=32000, tools=[TOOL],
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "record_page"},
            messages=[{"role": "user", "content": user}],
        )

    @staticmethod
    def parse(resp) -> dict:
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input)
                for key in ("productions", "detail_links", "listing_links"):
                    if key in data:
                        data[key] = _as_list(data[key])
                if truncated:
                    data["notes"] = ((data.get("notes") or "") + " [answer cut off: page too long]").strip()
                    data["_truncated"] = True
                return data
        return {"page_status": "not_a_schedule", "productions": [], "detail_links": [], "_truncated": True,
                "notes": "no answer (page too long)" if truncated else "no answer"}

    def read(self, snap, source: dict, venues: list[dict], role: str) -> dict:
        if self.out_of_credit:
            raise OutOfCredit("credit already used up in this run")
        try:
            resp = self.client.messages.create(**self.request_params(snap, source, venues, role))
        except anthropic.APIStatusError as e:
            if _credit_error(e):
                self.out_of_credit = True
                raise OutOfCredit(str(e)) from e
            raise
        self.calls += 1
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        return self.parse(resp)


class Batch:
    """Anthropic Message Batches: pages sent in bundles, answered within the hour, at half price.
    Bundles can be sent while browsing goes on (submit) and their answers picked up as they finish (poll),
    so each site is saved as soon as its own pages are answered."""

    CHUNK = 800            # requests per batch (keeps each batch well under the 256 MB limit)
    POLL_S = 60            # how often to ask whether a batch has finished

    def __init__(self, ex: Extractor):
        self.ex = ex
        self.requests: list[str] = []            # keys of every page added
        self.context: dict[str, object] = {}
        self._unsent: list[dict] = []
        self._open: dict[str, list[str]] = {}    # batch id -> keys
        self._failed: list[str] = []             # could not be sent (credit ran out)
        self._last_poll = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.submitted = 0
        self.out_of_credit = False

    def add(self, params: dict, context) -> str:
        key = f"p{len(self.requests):06d}"
        self.requests.append(key)
        self._unsent.append({"custom_id": key, "params": params})
        self.context[key] = context
        return key

    @property
    def unsent(self) -> int:
        return len(self._unsent)

    @property
    def busy(self) -> bool:
        return bool(self._unsent or self._open or self._failed)

    def submit(self) -> None:
        """Send every page added since the last call."""
        while self._unsent:
            if self.ex.out_of_credit:
                self.out_of_credit = True
                self._failed += [r["custom_id"] for r in self._unsent]
                self._unsent = []
                return
            chunk = self._unsent[: self.CHUNK]
            try:
                bid = self.ex.client.messages.batches.create(requests=chunk).id
            except anthropic.APIStatusError as e:
                if _credit_error(e):
                    self.out_of_credit = self.ex.out_of_credit = True
                    print("batch: the Anthropic credit is used up; these pages wait for the next run", flush=True)
                    continue
                raise
            self._open[bid] = [r["custom_id"] for r in chunk]
            self._unsent = self._unsent[self.CHUNK:]
            self.submitted += 1
            print(f"batch {bid}: {len(chunk)} pages sent", flush=True)

    def poll(self, force: bool = False) -> list[tuple[str, dict | None]]:
        """(key, answer) for every page whose batch has finished since the last call; answer None = no answer."""
        done = [(k, None) for k in self._failed]
        self._failed = []
        if not self._open or (not force and time.time() - self._last_poll < self.POLL_S):
            return done
        self._last_poll = time.time()
        for bid in list(self._open):
            try:
                ended = self.ex.client.messages.batches.retrieve(bid).processing_status == "ended"
            except Exception as e:
                print(f"batch {bid}: status check failed ({type(e).__name__}); trying again later", flush=True)
                continue
            if ended:
                done += self._collect(bid)
        return done

    def _collect(self, bid: str) -> list[tuple[str, dict | None]]:
        keys = self._open.pop(bid)
        got = {}
        try:
            for entry in self.ex.client.messages.batches.results(bid):
                if entry.result.type == "errored" and _credit_error(Exception(str(getattr(entry.result, "error", "")))):
                    self.out_of_credit = self.ex.out_of_credit = True
                if entry.result.type != "succeeded":
                    continue
                msg = entry.result.message
                self.calls += 1
                self.input_tokens += msg.usage.input_tokens
                self.output_tokens += msg.usage.output_tokens
                got[entry.custom_id] = self.ex.parse(msg)
        except Exception as e:
            print(f"batch {bid}: could not fetch all answers ({type(e).__name__}: {str(e)[:120]})", flush=True)
        print(f"batch {bid}: {len(got)} of {len(keys)} pages answered"
              + (" (the Anthropic credit ran out)" if self.out_of_credit else ""), flush=True)
        return [(k, got.get(k)) for k in keys]

    def wait(self, deadline: float, poll_s: float | None = None, until_any: bool = False) -> list[tuple[str, dict | None]]:
        """Send what is left and wait: for everything, or (until_any) until something has finished.
        At the deadline nothing is cancelled: batches still running stay open (see leave_open). Their answers are
        paid for either way, so the next run picks them up instead of paying for the pages a second time."""
        poll_s = self.POLL_S if poll_s is None else poll_s
        self.submit()
        out = self.poll(force=True)
        while self._open and not (until_any and out):
            if time.time() > deadline:
                break
            time.sleep(poll_s)
            out += self.poll(force=True)
        return out

    def drop_unsent(self) -> list[str]:
        """Pages added but never sent (the run ended first): not billed; they are simply read next run."""
        keys = [r["custom_id"] for r in self._unsent] + self._failed
        self._unsent, self._failed = [], []
        return keys

    def leave_open(self) -> dict[str, list[str]]:
        """Batches still running (batch id -> keys). They are kept by Anthropic for 29 days; the caller records them."""
        left, self._open = self._open, {}
        return left

    def run(self, deadline: float, poll_s: float | None = None) -> list[tuple[str, dict | None]]:
        """Send everything and wait for all of it."""
        return self.wait(deadline, poll_s)


# Words that change from day to day without the programme changing (ticket availability, "today" labels).
VOLATILE = re.compile(
    r"(sold ?out|few (?:tickets|seats|places) left|last (?:tickets|seats|places)|low availability|limited availability|"
    r"book now|buy tickets|tickets available|waiting ?list|udsolgt|få billetter|ausverkauft|restkarten|wenige karten|"
    r"\bcomplet\b|dernières places|uitverkocht|laatste kaarten|esaurito|ultimi posti|agotado|últimas entradas|slutsålt|"
    r"få biljetter|utsolgt|loppuunmyyty|wyprzedane|vyprodáno|elfogyott|esgotado|"
    r"\d+\s+(?:seats|tickets|places|plätze|pladser|posti|plazas|platser|plaatsen))", re.I)
DAY_LABEL = re.compile(r"^(today|tomorrow|tonight|heute|morgen|aujourd'hui|demain|ce soir|vandaag|vanavond|oggi|domani|"
                       r"stasera|hoy|mañana|esta noche|i dag|i morgen|idag|imorgon|tänään|huomenna|dziś|jutro|dnes|zítra)$", re.I)


def page_hash(snap) -> str:
    """Fingerprint of what matters on a page: its lines (in any order), without ticket-availability noise.
    Same fingerprint means the programme did not change, so last time's reading is reused for free."""
    import hashlib
    lines = set()
    for line in snap.text[: config.PAGE_TEXT_LIMIT].splitlines():
        text = " ".join(VOLATILE.sub(" ", line.lower()).split()).strip(" -|·•,:")
        if len(text) < 3 or DAY_LABEL.match(text):
            continue
        lines.add(text)
    events = [{k: v for k, v in e.items() if k not in ("offers", "remainingAttendeeCapacity")} for e in (snap.events or [])
              if isinstance(e, dict)]
    body = "\n".join(sorted(lines)) + json.dumps(events, sort_keys=True, ensure_ascii=False, default=str)[:20000]
    feeds = getattr(snap, "feeds", None) or []
    if feeds:
        body += "\nFEEDS:" + " ".join(sorted({d for f in feeds for d in DATE_IN_FEED.findall(f.get("text", ""))}))
    if getattr(snap, "sitemap", None):
        body += "\nSITEMAP:" + " ".join(sorted(snap.sitemap))
    return hashlib.sha256(body.encode()).hexdigest()


DATE_IN_FEED = re.compile(r"20\d\d-[01]\d-[0-3]\d(?:[T ][0-2]\d:[0-5]\d)?")


def runs_of(data: dict) -> list[dict]:
    """Runs whose individual dates are not all listed, with their production's details."""
    out = []
    for prod in _as_list(data.get("productions")):
        if not isinstance(prod, dict):
            continue
        for r in _as_list(prod.get("runs")):
            if not isinstance(r, dict):
                continue
            out.append({"title": (prod.get("title") or "").strip(), "work": prod.get("work"), "company": prod.get("company"),
                        "choreographer": prod.get("choreographer"), "image_url": prod.get("image_url"),
                        "description": prod.get("description"), "detail_url": prod.get("detail_url"),
                        "venue": (r.get("venue") or "").strip() or None, "city": (r.get("city") or "").strip() or None,
                        "country": (r.get("country") or "").strip() or None,
                        "start": r.get("start") if DATE_RE.match(r.get("start") or "") else None,
                        "end": r.get("end") if DATE_RE.match(r.get("end") or "") else None,
                        "expected": r.get("expected_performances") if isinstance(r.get("expected_performances"), int) else None,
                        "dates_page_url": r.get("dates_page_url") or None, "venue_event_url": r.get("venue_event_url") or None})
    return [r for r in out if r["title"]]


def _as_list(v) -> list:
    """The model sometimes returns a list as a JSON string: read it back as a list; anything else becomes []."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    if isinstance(v, dict):
        v = [v]
    return v if isinstance(v, list) else []


def flatten(data: dict) -> list[dict]:
    """productions -> one flat item per performance, in the shape clean_performance expects."""
    items = []
    for prod in _as_list(data.get("productions")):
        if not isinstance(prod, dict):
            continue
        base = {k: v for k, v in prod.items() if k != "performances"}
        for perf in _as_list(prod.get("performances")):
            if isinstance(perf, dict):
                items.append({**base, **perf})
    return items


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_performance(p: dict, today: dt.date) -> dict | None:
    """Validate one extracted item; returns None if it cannot be used."""
    title = (p.get("title") or "").strip()
    date = (p.get("date") or "").strip()
    if not title or not DATE_RE.match(date):
        return None
    try:
        if dt.date.fromisoformat(date) < today:
            return None
    except ValueError:
        return None
    time_ = (p.get("time") or "").strip() or None
    if time_ and not TIME_RE.match(time_):
        time_ = None
    tags = [t for t in (p.get("tags") or []) if isinstance(t, str) and t.strip()]
    if not any(t in config.GENRE_TAGS for t in tags):
        tags = ["Contemporary"] + tags
    desc = (p.get("description") or "").strip() or None
    if desc and len(desc) > 600:
        desc = desc[:597].rsplit(" ", 1)[0] + "..."
    pos = p.get("program_position")
    return {
        "title": title,
        "original_title": (p.get("original_title") or "").strip() or None,
        "work": (p.get("work") or "").strip() or None,
        "work_is_story": bool(p.get("work_is_story")),
        "choreographer": (p.get("choreographer") or "").strip() or None,
        "company": (p.get("company") or "").strip() or None,
        "venue": (p.get("venue") or "").strip() or None,
        "city": (p.get("city") or "").strip() or None,
        "country": (p.get("country") or "").strip() or None,
        "date": date,
        "time": time_,
        "tags": list(dict.fromkeys(tags)),
        "program": (p.get("program") or "").strip() or None,
        "program_position": pos if isinstance(pos, int) and pos > 0 else None,
        "ticket_url": (p.get("ticket_url") or "").strip() or None,
        "image_url": (p.get("image_url") or "").strip() or None,
        "description": desc,
        "cancelled": bool(p.get("cancelled")),
        "detail_url": (p.get("detail_url") or "").strip() or None,
        "timezone": _valid_tz(p.get("timezone")),
        "date_source": "pattern" if p.get("date_basis") == "pattern" else "listed",
    }


def _valid_tz(name) -> str | None:
    if not isinstance(name, str) or "/" not in name:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name.strip())
        return name.strip()
    except Exception:
        return None


def merge(items: list[dict], page_text: str = "") -> list[dict]:
    """Keep one item per piece, venue, date and time; a dated-and-timed item beats one without a time.
    Also removes the copies a reading can produce: an item without venue when the same piece and date is also
    given with a venue, and a time exactly 1 or 2 hours off another time of the same piece, venue and date
    (a UTC copy of the local time) when only one of the two times is written in the page text."""
    best: dict = {}
    for it in items:
        key = (it["title"].lower(), (it["venue"] or "").lower(), it["date"], it["program_position"] or 0)
        cur = best.get(key)
        if cur is None:
            best[key] = [it]
            continue
        timed = [c for c in cur if c["time"]]
        if it["time"]:
            if any(c["time"] == it["time"] for c in timed):
                continue
            best[key] = timed + [it]          # drop untimed placeholders once a time is known
        elif not timed:
            continue                          # keep the first untimed one

    # an item without venue is a copy when the same piece and date is also listed with a venue
    with_venue = {(k[0], k[2], k[3]) for k in best if k[1]}
    for k in [k for k in best if not k[1] and (k[0], k[2], k[3]) in with_venue]:
        del best[k]

    out = []
    for v in best.values():
        out.extend(_drop_zone_twins(v, page_text) if len(v) > 1 else v)
    return out


def _minutes(t: str) -> int:
    h, m = t.split(":")[:2]
    return int(h) * 60 + int(m)


def time_seen(t: str | None, text: str) -> bool:
    """Is this time written in the page text (19:30, 19.30, 19h30, 7:30 pm)?"""
    return bool(t) and _seen(t, text or "")


def _seen(t: str, text: str) -> bool:
    """Is this time written in the page text (19:30, 19.30, 19h30, 7:30 pm)?"""
    if not text:
        return False
    h, m = (int(x) for x in t.split(":")[:2])
    forms = [f"{h}:{m:02d}", f"{h:02d}:{m:02d}", f"{h}.{m:02d}", f"{h:02d}.{m:02d}", f"{h}h{m:02d}", f"{h}h"]
    if h > 12:
        forms += [f"{h - 12}:{m:02d} pm", f"{h - 12}:{m:02d}pm", f"{h - 12}.{m:02d} pm", f"{h - 12} pm", f"{h - 12}pm"]
    pat = "|".join(re.escape(f) for f in forms if not (f.endswith("h") and m))
    return re.search(rf"(?<![\d:.])(?:{pat})(?![\d])", text, re.I) is not None


def _drop_zone_twins(items: list[dict], page_text: str) -> list[dict]:
    """Of two times 60 or 120 minutes apart for one piece, venue and date, drop the one the page text does not show."""
    keep = list(items)
    for a in items:
        for b in items:
            if a is b or a not in keep or b not in keep or not a["time"] or not b["time"]:
                continue
            if abs(_minutes(a["time"]) - _minutes(b["time"])) in (60, 120):
                sa = a.get("time_seen") or _seen(a["time"], page_text)
                sb = b.get("time_seen") or _seen(b["time"], page_text)
                if sa and not sb:
                    keep.remove(b)
                elif sb and not sa:
                    keep.remove(a)
    return keep
