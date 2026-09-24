"""Ask Claude to read one page and return structured dance performances."""
import datetime as dt
import json
import re

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
                             "description": "URLs of dance productions on this site whose individual dates or times are NOT on this page and must be opened."},
            "next_page_url": {"type": ["string", "null"], "description": "Next page of this listing (pagination or next month), if any."},
            "schedule_url_guess": {"type": ["string", "null"], "description": "If this is not the schedule, the link most likely to be the dance/ballet programme or full calendar."},
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
Always answer by calling record_page."""


class Extractor:
    def __init__(self, model: str | None = None):
        # generous retries: new Anthropic accounts have low per-minute limits; the SDK waits as told by the API
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=10, timeout=600)
        self.model = model or config.MODEL
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.cache_hits = 0

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
        resp = self.client.messages.create(**self.request_params(snap, source, venues, role))
        self.calls += 1
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        return self.parse(resp)


class Batch:
    """Anthropic Message Batches: many pages sent at once, answered within the hour, at half price."""

    CHUNK = 800            # requests per batch (keeps each batch well under the 256 MB limit)

    def __init__(self, ex: Extractor):
        self.ex = ex
        self.requests: list[dict] = []
        self.context: dict[str, object] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, params: dict, context) -> str:
        key = f"p{len(self.requests):06d}"
        self.requests.append({"custom_id": key, "params": params})
        self.context[key] = context
        return key

    def run(self, deadline: float, poll_s: float = 60) -> dict:
        """Submit, wait until done or the deadline, and return {key: parsed answer}. Missing keys failed."""
        import time
        if not self.requests:
            return {}
        client = self.ex.client
        ids = [client.messages.batches.create(requests=self.requests[i:i + self.CHUNK]).id
               for i in range(0, len(self.requests), self.CHUNK)]
        print(f"batch: {len(self.requests)} pages in {len(ids)} batch(es): {', '.join(ids)}", flush=True)
        pending = set(ids)
        while pending:
            for bid in list(pending):
                if client.messages.batches.retrieve(bid).processing_status == "ended":
                    pending.discard(bid)
            if not pending:
                break
            if time.time() > deadline:
                print("batch: out of time, cancelling what is left", flush=True)
                for bid in pending:
                    try:
                        client.messages.batches.cancel(bid)
                    except Exception:
                        pass
                stop = time.time() + 600
                while pending and time.time() < stop:
                    for bid in list(pending):
                        if client.messages.batches.retrieve(bid).processing_status == "ended":
                            pending.discard(bid)
                    time.sleep(15)
                break
            time.sleep(poll_s)
        answers = {}
        for bid in ids:
            if bid in pending:
                continue
            for entry in client.messages.batches.results(bid):
                if entry.result.type != "succeeded":
                    continue
                msg = entry.result.message
                self.calls += 1
                self.input_tokens += msg.usage.input_tokens
                self.output_tokens += msg.usage.output_tokens
                answers[entry.custom_id] = self.ex.parse(msg)
        print(f"batch: {len(answers)} of {len(self.requests)} pages answered", flush=True)
        return answers


def page_hash(snap) -> str:
    """Fingerprint of what Claude would be shown; same fingerprint means the page did not change."""
    import hashlib
    body = snap.text[: config.PAGE_TEXT_LIMIT] + json.dumps(snap.events, sort_keys=True, ensure_ascii=False)[:20000]
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
