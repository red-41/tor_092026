"""Ask Claude to read one page and return structured dance performances."""
import datetime as dt
import json
import re
import time

import anthropic

import config

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
                                },
                                "required": ["date"],
                            },
                        },
                    },
                    "required": ["title", "tags", "performances"],
                },
            },
            "detail_links": {"type": "array", "items": {"type": "string"},
                             "description": "URLs of single dance productions on this site whose individual dates or times are NOT on this page and must be opened."},
            "listing_links": {"type": "array", "items": {"type": "string"},
                              "description": "Up to 3 URLs on this site of pages that list several dance productions: the ballet or dance "
                                             "programme, a dance category or filter, the season overview or the full calendar. "
                                             "Only when this page does not already show that list. Never single productions."},
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
- Leave out folk, traditional and ethnic dance (folk ensembles, flamenco, Irish dance, Indian classical dance, tango shows and similar).
- Only performances on or after today's date.
- Give every date and time of a production in its performances list. A run of 12 dates has 12 entries.
- Mixed bill (several pieces in one evening): one production entry per piece, each with the evening's dates, plus program and program_position.
- Titles in English (use the site's English version if there is one), with original_title for the native title when different.
- Genre tags: always at least one of Classical, Neoclassical, Contemporary.
- If the time is not published, set time to null. Never guess a time.
- description: your own words, 1 to 2 sentences, never copied.
- Use absolute URLs. Prefer the booking link for that exact date as ticket_url, else the production page.
- If dates or times are only on production pages, list those pages in detail_links (dance productions only).
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
        events = json.dumps(snap.events, ensure_ascii=False)[:20000] if snap.events else ""
        user = (
            f"Today: {today}\n"
            f"Source: {source.get('name')} ({source.get('kind')}), {source.get('city')}, {source.get('country')}. "
            f"Time zone {source.get('timezone')}.\n"
            f"This page is a {role}.\nURL: {snap.final_url}\nPage title: {snap.title}\n"
            f"og:image: {snap.og_image}\n\n"
            f"Known venues in this country (reuse exact names): {'; '.join(venue_names) or 'none'}\n\n"
            + (f"Structured event data found in the page (schema.org):\n{events}\n\n" if events else "")
            + "Links on the page (text | url):\n" + "\n".join(f"{t} | {h}" for t, h in same_site) + "\n\n"
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
    GIVE_UP_S = 300        # after the deadline: time allowed for cancelled batches to stop

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
        At the deadline, cancel what is still running, give it a few minutes, and return None for the rest."""
        poll_s = self.POLL_S if poll_s is None else poll_s
        self.submit()
        out = self.poll(force=True)
        while self._open and not (until_any and out):
            if time.time() > deadline:
                out += self._give_up(poll_s)
                break
            time.sleep(poll_s)
            out += self.poll(force=True)
        return out

    def _give_up(self, poll_s: float) -> list[tuple[str, dict | None]]:
        print("batch: out of time, cancelling what is left", flush=True)
        for bid in self._open:
            try:
                self.ex.client.messages.batches.cancel(bid)
            except Exception:
                pass
        out, stop = [], time.time() + self.GIVE_UP_S
        while self._open and time.time() < stop:
            time.sleep(min(15, poll_s))
            out += self.poll(force=True)
        for bid in list(self._open):
            out += [(k, None) for k in self._open.pop(bid)]
        return out

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
    return hashlib.sha256(body.encode()).hexdigest()


def flatten(data: dict) -> list[dict]:
    """productions -> one flat item per performance, in the shape clean_performance expects."""
    items = []
    for prod in data.get("productions") or []:
        base = {k: v for k, v in prod.items() if k != "performances"}
        for perf in prod.get("performances") or []:
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
    }


def merge(items: list[dict]) -> list[dict]:
    """Keep one item per piece, venue, date and time; a dated-and-timed item beats one without a time."""
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
    out = []
    for v in best.values():
        out.extend(v)
    return out
