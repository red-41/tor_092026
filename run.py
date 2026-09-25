"""Collect upcoming dance performances from due sources and put them into the staging table.

Usage:
  python run.py --limit 60                 # next 60 due sources
  python run.py --priority 0 --limit 200   # only the tier 0 core
  python run.py --name "Sadler"            # one source by name
  python run.py --shard 2 --shards 12      # one of 12 parallel slices (GitHub matrix)
  python run.py --status blocked           # retry every blocked source (from a home computer)
  python run.py --dry                      # read and report, write nothing to the site's tables
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from types import SimpleNamespace
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import config
import db
from browse import Browser
from extract import Batch, Extractor, OutOfCredit, clean_performance, flatten, merge, page_hash

THIN = 150          # characters: below this a page has nothing to read


LISTING = "schedule or listing page"
DETAIL = "production page with dates and cast"


def read_page(ex: Extractor, snap, source: dict, venues: list, role: str, use_cache: bool, key: str | None = None) -> dict:
    """Claude's reading of a page, reusing last time's reading when the page has not changed."""
    key = key or snap.final_url
    h = page_hash(snap)
    if use_cache:
        hit = db.cache_get(key, role, h, ex.model)
        if hit is not None:
            ex.cache_hits += 1
            return hit
    data = ex.read(snap, source, venues, role=role)
    if not data.get("_truncated"):
        db.cache_put(key, role, h, ex.model, data)
    return data


def reusable(prev: dict | None, today: dt.date) -> bool:
    """A production page read before is not opened again unless its next show is near or the reading is old."""
    if not prev or not isinstance(prev.get("result"), dict):
        return False
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(prev["updated_at"])
    except Exception:
        return False
    if age.days >= config.DETAIL_REFRESH_DAYS:
        return False
    dates = []
    for p in flatten(prev["result"]):
        try:
            dates.append(dt.date.fromisoformat((p.get("date") or "").strip()))
        except ValueError:
            pass
    if not dates:
        return False                      # no dates known yet: look again, they may be out now
    future = [d for d in dates if d >= today]
    if not future:
        return True                       # the run is over
    return min(future) > today + dt.timedelta(days=config.DETAIL_SOON_DAYS)


def _skip_reason(snap) -> str:
    if snap.error:
        return snap.error
    if snap.blocked:
        return f"blocked ({snap.blocked})"
    if len(snap.text) < THIN and not snap.events:
        return f"empty page ({len(snap.text)} characters)"
    return ""


def apply_detail(result: dict, data: dict, snap, url: str) -> None:
    """Add the performances from one production page to a source's result.
    If the 'production page' turned out to be an overview (a ballet programme listing its shows), its show pages
    are opened too, one level deep."""
    st = result["_state"]
    today = st["today"]
    if data.get("notes"):
        result["notes"].append(data["notes"])
    for p in flatten(data):
        c = clean_performance(p, today)
        if c:
            c["image_url"] = c["image_url"] or (snap.og_image if snap else None) or None
            c["ticket_url"] = c["ticket_url"] or (snap.final_url if snap else url)
            result["items"].append(c)
    if data.get("page_status") == "listing_without_dates" and url not in st["deep"]:
        new = [u for u in (data.get("detail_links") or []) if isinstance(u, str) and u not in st["details"]]
        if new:
            st["details"] += new
            st["deep"].update(new)
            st["more"] = True


def _light(snap):
    """What a production page needs to keep while its answer is pending (not the whole page)."""
    return SimpleNamespace(og_image=snap.og_image, final_url=snap.final_url)


def open_page(br: Browser, result: dict, url: str):
    snap = br.load(url)
    result["pages"] += 1
    if snap.screenshot:
        result["screens"].append(snap.screenshot)
    if snap.cookie not in ("none", "refused"):
        result["cookies"].append(snap.cookie)
    reason = _skip_reason(snap)
    if reason:
        if snap.blocked:
            result["blocked"].append(snap.blocked)
        result["notes"].append(f"{url}: {reason}")
        return None
    return snap


def handle_listing(result: dict, snap, data: dict) -> None:
    """Use Claude's reading of a listing page: performances, production pages to open, next pages."""
    st = result["_state"]
    status = data.get("page_status")
    st["statuses"].append(status)
    guess = data.get("schedule_url_guess")
    if status in ("not_a_schedule", "no_dance_programme") and guess and st["listings_read"] == 0:
        if guess not in st["visited"] and guess not in st["queue"]:
            result["schedule_url"] = guess               # the real dance programme: start there next time
            st["queue"].insert(0, guess)
        if status == "not_a_schedule":
            return
    st["listings_read"] += 1
    if st["listings_read"] == 1 and not result["source"].get("schedule_url") and not result["schedule_url"]:
        result["schedule_url"] = snap.final_url
    for p in flatten(data):
        c = clean_performance(p, st["today"])
        if c:
            result["items"].append(c)
    st["details"] += [u for u in data.get("detail_links", []) if isinstance(u, str) and u not in st["details"]]
    for u in (data.get("listing_links") or [])[:3]:
        if isinstance(u, str) and u not in st["visited"] and u not in st["queue"]:
            st["queue"].append(u)
    nxt = data.get("next_page_url")
    if nxt and nxt not in st["visited"] and nxt not in st["queue"]:
        st["queue"].append(nxt)
    if data.get("notes"):
        result["notes"].append(data["notes"])


def start_source(br: Browser, ex: Extractor, source: dict, use_cache: bool = True, batch: Batch | None = None) -> dict:
    """Open a source's schedule page. Its reading comes from the cache, from Claude now, or (batch) later."""
    tz = ZoneInfo(source.get("timezone") or "Europe/Paris")
    prio = source.get("priority")
    result = {"source": source, "items": [], "pages": 0, "status": "ok", "notes": [], "schedule_url": None,
              "blocked": [], "screens": [], "cookies": [], "check": False, "reused": 0, "pending": 0, "minutes": 0.0,
              "_state": {"today": dt.datetime.now(tz).date(), "statuses": [], "readable": 0, "listings_read": 0,
                         "out_of_time": False, "details": [], "deep": set(), "more": False, "visited": set(),
                         "queue": [], "first": None, "browse_s": 0.0,
                         "venues": db.known_venues(source.get("country")),
                         "cap": config.DETAIL_CAP.get(2 if prio is None else prio, config.MAX_DETAIL_PAGES)}}
    st = result["_state"]
    t0 = time.monotonic()
    start = source.get("schedule_url") or source.get("website")
    st["visited"].add(start)
    snap = open_page(br, result, start)
    if snap:
        st["readable"] += 1
        h = page_hash(snap)
        hit = db.cache_get(snap.final_url, LISTING, h, ex.model) if use_cache else None
        if hit is not None:
            ex.cache_hits += 1
            st["first"] = (snap, hit)
        elif batch is not None:
            batch.add(ex.request_params(snap, source, st["venues"], LISTING), ("listing", result, snap, snap.final_url, h))
            result["pending"] += 1
            st["first"] = (snap, None)
        else:
            st["first"] = (snap, read_page(ex, snap, source, st["venues"], LISTING, use_cache))
    _spent(result, t0)
    return result


def _spent(result: dict, t0: float) -> None:
    st = result["_state"]
    st["browse_s"] += time.monotonic() - t0
    result["minutes"] = round(st["browse_s"] / 60, 1)


def continue_source(br: Browser, ex: Extractor, result: dict, use_cache: bool = True, batch: Batch | None = None,
                    max_minutes: float | None = None, stop_at: float | None = None) -> dict:
    """Everything after the first listing page: more listing pages, then the production pages.
    max_minutes: browsing time allowed for this source; stop_at: clock time after which no page is opened."""
    t0 = time.monotonic()
    source, st = result["source"], result["_state"]
    if max_minutes is not None:
        st["max_s"] = max_minutes * 60
    venues, visited, queue = st["venues"], st["visited"], st["queue"]
    if st["first"]:
        snap, data = st["first"]
        if data is None:                                    # the batch gave no answer: read it now
            data = read_page(ex, snap, source, venues, LISTING, use_cache)
        st["first"] = None
        handle_listing(result, snap, data)

    # further listing pages (the dance programme, pagination, next months): answered straight away
    while queue and result["pages"] < config.MAX_LISTING_PAGES:
        if stop_at and time.time() > stop_at:
            break
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        snap = open_page(br, result, url)
        if not snap:
            continue
        st["readable"] += 1
        handle_listing(result, snap, read_page(ex, snap, source, venues, LISTING, use_cache))
    _spent(result, t0)
    open_details(br, ex, result, use_cache, batch, stop_at)
    return result


def open_details(br: Browser, ex: Extractor, result: dict, use_cache: bool = True, batch: Batch | None = None,
                 stop_at: float | None = None) -> None:
    """Open the production pages not opened yet (up to the cap): reuse, cache, batch, or read straight away."""
    source, st = result["source"], result["_state"]
    limit_s = st.get("max_s", config.MAX_SOURCE_MINUTES * 60)
    t0 = time.monotonic()
    try:
        while True:
            todo = [u for u in st["details"][: st["cap"]] if u not in st["visited"]]
            if not todo:
                return
            for url in todo:
                if use_cache:
                    prev = db.cache_peek(url, DETAIL)
                    if reusable(prev, st["today"]):      # read before, next show not soon: no need to open it
                        st["visited"].add(url)
                        result["reused"] += 1
                        apply_detail(result, prev["result"], None, url)
                        continue
                if st["browse_s"] + (time.monotonic() - t0) > limit_s or (stop_at and time.time() > stop_at):
                    if not st["out_of_time"]:
                        st["out_of_time"] = True
                        result["notes"].append(f"out of time after {result['pages']} pages; the rest next run")
                    return
                st["visited"].add(url)
                snap = open_page(br, result, url)
                if not snap:
                    continue
                st["readable"] += 1
                if batch is None:
                    apply_detail(result, read_page(ex, snap, source, st["venues"], DETAIL, use_cache, key=url), snap, url)
                    continue
                h = page_hash(snap)
                hit = db.cache_get(url, DETAIL, h, ex.model) if use_cache else None
                if hit is not None:
                    ex.cache_hits += 1
                    apply_detail(result, hit, snap, url)
                    continue
                batch.add(ex.request_params(snap, source, st["venues"], DETAIL), ("detail", result, _light(snap), url, h))
                result["pending"] += 1
    finally:
        _spent(result, t0)


def collect_source(br: Browser, ex: Extractor, source: dict, use_cache: bool = True,
                   max_minutes: float | None = None, batch: Batch | None = None) -> dict:
    """One source start to finish; listing pages answered straight away, production pages through `batch` if given."""
    res = continue_source(br, ex, start_source(br, ex, source, use_cache), use_cache, batch, max_minutes)
    if not res["pending"]:
        finalize(res)
    return res


def finalize(result: dict) -> dict:
    """Merge, decide the status and compare with what Saffitt already has."""
    source, st = result["source"], result["_state"]
    details, cap = st["details"], st["cap"]
    result["items"] = merge(result["items"])
    if len(details) > cap:
        result["notes"].append(f"only {cap} of {len(details)} production pages read")
    if result["items"]:
        incomplete = len(details) > cap or st["out_of_time"] or result["blocked"] or st.get("unanswered")
        result["status"] = "partial" if incomplete else "ok"
    elif result["blocked"] and not st["readable"]:
        result["status"] = "blocked"
    elif "not_published_yet" in st["statuses"]:
        result["status"] = "waiting"
    elif st["statuses"] and all(x in ("no_dance_programme", "not_a_schedule") for x in st["statuses"]):
        result["status"] = "no_dance"
    else:
        result["status"] = "failed"

    # sanity check against what is already on Saffitt from this source (nothing is ever removed)
    upcoming = db.upcoming_count(source["id"])
    result["upcoming_before"] = upcoming
    if upcoming and len(result["items"]) < 0.5 * upcoming:
        result["check"] = True
        result["notes"].insert(0, f"CHECK: found {len(result['items'])}, but {upcoming} upcoming from this source "
                                  f"are already on Saffitt (kept)")
        if result["status"] == "no_dance":
            result["status"] = "failed"
    result["read_method"] = "detail_pages" if details else ("paged_list" if result["pages"] > 1 else "single_page")
    return result


def apply_batch(batch: Batch, finished: list, ex: Extractor) -> list[dict]:
    """Hand batch answers back to their sources. Returns the sources that got answers."""
    touched = []
    for key, data in finished:
        ctx = batch.context.pop(key, None)
        if ctx is None:
            continue
        kind, result, snap, url, h = ctx
        result["pending"] -= 1
        if not any(r is result for r in touched):
            touched.append(result)
        role = LISTING if kind == "listing" else DETAIL
        if data is not None and not data.get("_truncated"):
            db.cache_put(url, role, h, ex.model, data)
        if kind == "listing":
            result["_state"]["first"] = (snap, data)        # None: read it straight away in continue_source
            continue
        if data is None:
            result["_state"]["unanswered"] = True
            result["notes"].append(f"{url}: no answer from the batch; next run")
            continue
        apply_detail(result, data, snap, url)
    return touched


def _timezone(item: dict, source: dict) -> str | None:
    """The venue's time zone: from the performance's country when it is abroad, else the source's."""
    country = (item.get("country") or "").strip()
    if country and country != source.get("country"):
        tz = config.TZ_BY_COUNTRY.get(country)
        if tz:
            return tz
    return source.get("timezone")


def recheck_days(source: dict, found: int) -> int:
    p = source.get("priority")
    days = config.RECHECK_DAYS.get(2 if p is None else p, 14)
    if source.get("kind") == "Festival" and found == 0:
        days = max(days, config.FESTIVAL_WAIT_DAYS)
    return days


def to_staging(result: dict, run_date: str) -> list[dict]:
    s = result["source"]
    host = urlparse(s.get("website") or "").netloc
    rows = []
    for it in result["items"]:
        rows.append({
            "source_id": s["id"], "source": host,
            "title": it["title"], "original_title": it["original_title"],
            "work": it["work"], "work_is_story": it["work_is_story"],
            "choreographer": it["choreographer"], "company": it["company"],
            "venue": it["venue"], "city": it["city"] or s.get("city"), "country": it["country"] or s.get("country"),
            "timezone": _timezone(it, s),
            "performance_date": it["date"], "performance_time": it["time"], "time_tbc": it["time"] is None,
            "tags": it["tags"], "genre": it["tags"][0],
            "program": it["program"], "program_position": it["program_position"],
            "tickets_url": it["ticket_url"], "image_url": it["image_url"], "description": it["description"],
            "cancelled": it["cancelled"],
            "dedupe_key": db.dedupe_key(host, it, run_date), "status": "pending",
        })
    return rows


def main(argv=None):
    started = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--priority", type=int)
    ap.add_argument("--name")
    ap.add_argument("--status", help="every source in this status, due or not (e.g. blocked)")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--model")
    ap.add_argument("--no-cache", action="store_true", help="send every page to Claude even if unchanged")
    ap.add_argument("--no-batch", action="store_true", help="answer every page straight away (full price)")
    ap.add_argument("--dry", action="store_true", help="read and report only; write nothing to staging or sources")
    args = ap.parse_args(argv)

    run_date = dt.date.today().isoformat()
    use_cache = not args.no_cache
    sources = db.due_sources(args.limit, args.priority, args.name, args.shard, args.shards, status=args.status)
    ex = Extractor(args.model)
    if sources:
        ex.check()
    use_batch = config.USE_BATCH and not args.no_batch
    listings, details = (Batch(ex), Batch(ex)) if use_batch else (None, None)
    job_end = started + config.JOB_MINUTES * 60
    stop_pages = job_end - config.DETAIL_RESERVE_MINUTES * 60     # no page is opened after this
    report, results, skipped = [], [], []
    os.makedirs("reports", exist_ok=True)
    print(f"{len(sources)} sources due (shard {args.shard + 1}/{args.shards})"
          + (", pages read through the half-price Batch API" if use_batch else ""), flush=True)

    def finish(res):
        """Save a source whose pages are all answered (or given up on)."""
        if res.get("saved"):
            return
        res["saved"] = True
        if ex.out_of_credit and (res["_state"].get("unanswered") or res["_state"]["out_of_time"]):
            credit_stop(res)
            if not res["items"]:
                skipped.append(res["source"])     # nothing found yet: leave the source due, untouched
                return
        try:
            if "upcoming_before" not in res:
                finalize(res)
            report.append(save(res, args, run_date))
        except Exception as e:
            traceback.print_exc()
            report.append(save_failure(res["source"], e, args, run_date))

    def handle(finished):
        """Answers came back: apply them, open any new show pages they point to, save sources that are done."""
        for res in apply_batch(details, finished, ex):
            st = res["_state"]
            if st["more"] and not ex.out_of_credit:
                st["more"] = False
                try:
                    open_details(br, ex, res, use_cache, details, stop_pages)
                except Exception:
                    traceback.print_exc()
            if not res["pending"]:
                finish(res)

    with Browser(shots_dir="reports/screens", shots="problems") as br:
        # Round 1: open every schedule page. Unchanged pages reuse last week's reading; the rest go into one batch.
        for s in sources:
            if ex.out_of_credit:
                skipped.append(s)
                continue
            print(f"-> {s['name']}", flush=True)
            try:
                results.append(start_source(br, ex, s, use_cache, listings))
            except OutOfCredit:
                skipped.append(s)
            except Exception as e:
                traceback.print_exc()
                report.append(save_failure(s, e, args, run_date))
        if listings and listings.requests:
            apply_batch(listings, listings.run(deadline=min(started + config.LISTING_BATCH_MINUTES * 60, job_end)), ex)

        # Round 2: more listing pages, then the production pages. Those go out in small batches while browsing
        # continues, and every source is saved as soon as its own pages are answered.
        last_sent = time.time()
        for res in results:
            s, first = res["source"], res["_state"]["first"]
            if ex.out_of_credit and first and first[1] is None:
                skipped.append(s)                    # schedule page never read: leave the source due, untouched
                res["saved"] = True
                continue
            print(f"=> {s['name']}", flush=True)
            try:
                continue_source(br, ex, res, use_cache, details,
                                stop_at=time.time() if ex.out_of_credit else stop_pages)
            except OutOfCredit:
                res["_state"]["unanswered"] = True
            except Exception as e:
                traceback.print_exc()
                res["saved"] = True
                report.append(save_failure(s, e, args, run_date))
                continue
            if not res["pending"]:
                finish(res)
            if details:
                if details.unsent >= config.BATCH_SEND_AT or (details.unsent and time.time() - last_sent > config.BATCH_SEND_EVERY_S):
                    details.submit()
                    last_sent = time.time()
                handle(details.poll())

        # Wait for the last answers, saving sources as they complete.
        while details and details.busy:
            handle(details.wait(deadline=job_end, until_any=True))
        for res in results:
            finish(res)

    for s in skipped:
        report.append({"source": s["name"], "status": "not read", "performances": 0, "staged": 0, "pages": 0,
                       "reused": 0, "check": False, "blocked": [], "cookies": [], "screens": [], "minutes": 0,
                       "notes": ["the Anthropic credit ran out before this source; it stays due for the next run"]})
    write_report(report, ex, args, [b for b in (listings, details) if b], credit_out=ex.out_of_credit)
    return 0


def credit_stop(res: dict) -> None:
    """The credit ran out part way through a source: keep what was found, look again at the next run."""
    res["retry_soon"] = True
    res["_state"]["unanswered"] = True
    note = "the Anthropic credit ran out; the rest at the next run"
    if note not in res["notes"]:
        res["notes"].append(note)


def save(res: dict, args, run_date: str) -> dict:
    """Stage the performances, update the source, and record the outcome."""
    s = res["source"]
    rows = to_staging(res, run_date)
    staged = 0 if args.dry else db.stage(rows)
    days = 0 if res.get("retry_soon") else recheck_days(s, len(rows))    # credit ran out: due again straight away
    fields = {
        "status": res["status"],
        "next_check_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(),
        "notes": ((s.get("notes") or "").split(" || last run:")[0]
                  + f" || last run: {run_date}, {len(rows)} performances, {res['pages']} pages"
                  + (f", {'; '.join(res['notes'])[:300]}" if res["notes"] else "")),
    }
    if res["schedule_url"] and not s.get("schedule_url"):
        fields["schedule_url"] = res["schedule_url"]
    if not s.get("read_method"):
        fields["read_method"] = res["read_method"]
    if not args.dry:
        db.update_source(s["id"], fields)
    entry = {"source": s["name"], "status": res["status"], "performances": len(rows), "staged": staged,
             "pages": res["pages"], "reused": res.get("reused", 0), "check": res["check"],
             "upcoming_before": res.get("upcoming_before"), "blocked": sorted(set(res["blocked"])),
             "cookies": sorted(set(res["cookies"])), "screens": res["screens"][:5],
             "minutes": res.get("minutes", 0), "notes": res["notes"][:4]}
    _record(s, entry, args)
    return entry


def save_failure(s: dict, e: Exception, args, run_date: str) -> dict:
    if not args.dry:
        try:
            db.update_source(s["id"], {"status": "failed", "notes": (s.get("notes") or "").split(" || last run:")[0]
                                       + f" || last run: {run_date}, error {type(e).__name__}"})
        except Exception:
            pass
    entry = {"source": s["name"], "status": "failed", "performances": 0, "staged": 0, "pages": 0, "reused": 0,
             "check": False, "blocked": [], "cookies": [], "screens": [], "minutes": 0,
             "notes": [f"{type(e).__name__}: {str(e)[:200]}"]}
    _record(s, entry, args)
    return entry


def _record(s: dict, entry: dict, args) -> None:
    db.record_check({
        "kind": "collect", "source_id": s["id"], "source_name": s["name"],
        "url": s.get("schedule_url") or s.get("website"), "verdict": entry["status"],
        "blocked_by": ", ".join(entry["blocked"]) or None, "cookie_banner": ", ".join(entry["cookies"]) or None,
        "performances": entry["performances"], "pages": entry["pages"], "upcoming_before": entry.get("upcoming_before"),
        "needs_check": bool(entry["check"]), "minutes": entry["minutes"],
        "notes": "; ".join(entry["notes"])[:2000] or None,
        "screenshot": ", ".join(os.path.basename(x) for x in entry["screens"]) or None})
    with open(f"reports/progress-shard{args.shard}.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_report(report, ex, args, batches=(), credit_out=False):
    by = {}
    for r in report:
        by[r["status"]] = by.get(r["status"], 0) + 1
    total = sum(r["performances"] for r in report)
    problems = [r for r in report if r["status"] in ("blocked", "failed") or r["check"]]
    b_calls = sum(b.calls for b in batches)
    b_in = sum(b.input_tokens for b in batches)
    b_out = sum(b.output_tokens for b in batches)
    lines = [f"## Saffitt collector, shard {args.shard + 1}/{args.shards}" + (" (dry run)" if args.dry else "")]
    if credit_out:
        lines += ["", "**The Anthropic credit ran out during this run.** Everything found before that is saved. "
                  "Sources marked 'not read' or 'credit ran out' stay due: add credit in the Console, then run again.", ""]
    lines += [f"{len(report)} sources: " + ", ".join(f"{v} {k}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
              + f". {total} performances found.",
              f"Claude ({ex.model}): {b_calls} pages via the Batch API at half price ({b_in:,} tokens in, {b_out:,} out); "
              f"{ex.calls} pages at full price ({ex.input_tokens:,} in, {ex.output_tokens:,} out); "
              f"{ex.cache_hits} unchanged pages and {sum(r.get('reused', 0) for r in report)} production pages reused for free.",
              ""]
    if problems:
        lines += ["### Needs a look", "| Source | Status | Found | Already on site | Why |", "|---|---|---|---|---|"]
        for r in problems:
            why = "; ".join(r["notes"]).replace("|", "/")[:220]
            lines.append(f"| {r['source']} | {r['status']} | {r['performances']} | {r.get('upcoming_before') or 0} | {why} |")
        lines.append("")
    lines += ["### All sources", "| Source | Status | Found | Pages | Cookie banner | Minutes | Notes |",
              "|---|---|---|---|---|---|---|"]
    for r in report:
        notes = "; ".join(r["notes"]).replace("|", "/")[:160]
        lines.append(f"| {r['source']} | {r['status']} | {r['performances']} | {r['pages']} | "
                     f"{', '.join(r['cookies']) or '-'} | {r['minutes']} | {notes} |")
    md = "\n".join(lines)
    print(md)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    with open(f"reports/run-{stamp}-shard{args.shard}.md", "w") as f:
        f.write(md)
    with open(f"reports/run-{stamp}-shard{args.shard}.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")


if __name__ == "__main__":
    sys.exit(main())
