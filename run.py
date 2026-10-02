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
import hashlib
import json
import os
import re
import sys
import time
import traceback
from types import SimpleNamespace
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import config
import db
import lenses
from browse import Browser
from extract import Batch, Extractor, OutOfCredit, clean_performance, flatten, merge, page_hash, runs_of, time_seen

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
    if runs_of(prev["result"]) or _short_of_expected(prev["result"]):
        return False                      # some dates were missing last time: look again, they may be out now
    future = [d for d in dates if d >= today]
    if not future:
        return True                       # the run is over
    return min(future) > today + dt.timedelta(days=config.DETAIL_SOON_DAYS)


def _short_of_expected(data: dict) -> bool:
    exp = data.get("expected_performances")
    return isinstance(exp, int) and exp > len({(p.get("date"), p.get("time")) for p in flatten(data)})


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
    are opened too, one level deep. Runs with missing dates start a date hunt (tiers 0-1)."""
    st = result["_state"]
    today = st["today"]
    lens = st["hunt"].get(url)                  # this page was opened to find a run's dates
    if data.get("notes"):
        result["notes"].append(data["notes"])
    here = []
    for p in flatten(data):
        c = clean_performance(p, today)
        if c:
            c["time_seen"] = time_seen(c["time"], getattr(snap, "text", "") or "")
            c["image_url"] = c["image_url"] or (snap.og_image if snap else None) or None
            c["ticket_url"] = c["ticket_url"] or (snap.final_url if snap else url)
            if lens and c["date_source"] == "listed":
                c["date_source"] = lens
            result["items"].append(c)
            here.append(c)
    runs = runs_of(data)
    expected = data.get("expected_performances")
    if not runs and isinstance(expected, int) and here and len({(c["date"], c["time"]) for c in here}) < expected:
        first = here[0]                          # count check: fewer dates found than the page announces
        runs = [{"title": first["title"], "work": first["work"], "company": first["company"],
                 "choreographer": first["choreographer"], "image_url": first["image_url"], "description": first["description"],
                 "detail_url": url, "venue": first["venue"], "city": first["city"], "country": first["country"],
                 "start": min(c["date"] for c in here), "end": max(c["date"] for c in here), "expected": expected,
                 "dates_page_url": None, "venue_event_url": None}]
    for r in runs:
        r["detail_url"] = r.get("detail_url") or url
        add_run(result, r, lens or ("listing" if url == "listing" else "production page"))
        hunt(result, r)
    if data.get("page_status") == "listing_without_dates" and url not in st["deep"]:
        new = [u for u in (data.get("detail_links") or []) if isinstance(u, str) and u not in st["details"]]
        if new:
            st["details"] += new
            st["deep"].update(new)
            st["more"] = True


def _run_key(result: dict, r: dict) -> str:
    return "|".join([(r.get("title") or "").lower(), (r.get("venue") or "").lower(), r.get("start") or ""])


def add_run(result: dict, r: dict, lens: str) -> None:
    """Remember a run with missing dates (merging the same run seen on several pages)."""
    runs = result["_state"]["runs"]
    key = _run_key(result, r)
    cur = runs.get(key)
    if cur is None:
        runs[key] = cur = {**r, "lenses": []}
    else:
        for k, v in r.items():
            if v and not cur.get(k):
                cur[k] = v
    if lens not in cur["lenses"]:
        cur["lenses"].append(lens)


def hunt(result: dict, r: dict) -> None:
    """Date hunt (tiers 0-1): open the run's dates page and the host venue's own page, if not tried yet."""
    st = result["_state"]
    if not st["core"]:
        return
    cur = st["runs"][_run_key(result, r)]
    for field, lens in (("dates_page_url", "booking"), ("venue_event_url", "venue_site")):
        u = (r.get(field) or "").strip()
        if not u.startswith("http") or lenses.is_ticket_seller(u) or u in st["visited"] or u in st["hunt"]:
            continue
        if st["hunts_used"] >= config.HUNT_PAGES:
            if "hunt limit reached" not in cur["lenses"]:
                cur["lenses"].append("hunt limit reached")
            return
        st["hunts_used"] += 1
        st["hunt"][u] = lens
        st["hunt_queue"].append(u)
        st["more"] = True
        if lens not in cur["lenses"]:
            cur["lenses"].append(lens)


def _light(snap):
    """What a production page needs to keep while its answer is pending (not the whole page)."""
    return SimpleNamespace(og_image=snap.og_image, final_url=snap.final_url)


def open_page(br: Browser, result: dict, url: str):
    snap = br.load(url)
    if not snap.error and not snap.blocked and len(snap.text) < THIN and not snap.events:
        # an almost empty page is often a browser that has gone bad (several sites in a row came back with the
        # same 51 characters): start a fresh browser and try once more before calling the page empty
        try:
            br._restart()
            snap = br.load(url)
        except Exception:
            pass
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


def handle_listing(result: dict, snap, data: dict, url: str | None = None) -> None:
    """Use Claude's reading of a listing page: performances, production pages to open, next pages."""
    st = result["_state"]
    url = url or snap.final_url
    status = data.get("page_status")
    st["statuses"].append(status)
    guess = data.get("schedule_url_guess")
    if status in ("not_a_schedule", "no_dance_programme") and guess and st["listings_read"] == 0:
        if guess not in st["visited"] and guess not in st["queue"]:
            result["schedule_url"] = guess               # the real dance programme: start there next time
            st["queue"].insert(0, guess)
            st["origin"][guess] = "guess"
        if status == "not_a_schedule":
            return
    st["listings_read"] += 1
    if st["listings_read"] == 1 and not result["source"].get("schedule_url") and not result["schedule_url"]:
        result["schedule_url"] = snap.final_url
    found = 0
    for p in flatten(data):
        c = clean_performance(p, st["today"])
        if c:
            c["time_seen"] = time_seen(c["time"], getattr(snap, "text", "") or "")
            result["items"].append(c)
            st["listing_dates"].append(c["date"])
            found += 1
    new_details = [u for u in data.get("detail_links", []) if isinstance(u, str) and u not in st["details"]]
    st["details"] += new_details
    if st["origin"].get(url) == "listing_link" and (found or new_details):
        st["learned"].append(url)                        # another dance category that pays off: keep it as a start page
    for u in (data.get("listing_links") or [])[:4]:
        if isinstance(u, str) and u.startswith("http") and u not in st["visited"] and u not in st["queue"]:
            st["queue"].append(u)
            st["origin"].setdefault(u, "listing_link")
    if st["origin"].get(url) == "month":
        st["months_read"] += 1
        if found or new_details:
            # this month has shows: keep reading ahead, MONTH_LOOKAHEAD months past the last month with shows
            if url in st["month_list"]:
                queue_months(st, st["month_list"].index(url) + 1 + config.MONTH_LOOKAHEAD)
        else:
            st["months_empty"] += 1
    nxt = data.get("next_page_url")
    if (isinstance(nxt, str) and nxt.startswith("http") and nxt not in st["visited"] and nxt not in st["queue"]
            and lenses.same_address_key(nxt) not in st.setdefault("month_keys", set())):   # months: look-ahead below
        st["queue"].append(nxt)
        st["origin"].setdefault(nxt, "next")
        st["paged"] = True
        if st["core"] and not st["months_queued"]:
            # a calendar by month: read the next months a few at a time, and stop a few months after the last
            # month that has shows (most houses publish one to three months ahead, some the whole season)
            months = [m for m in lenses.month_series(nxt, st["today"], config.MONTHS_AHEAD)
                      if m not in st["visited"] and m not in st["queue"]]
            if months:
                st["months_queued"] = True
                st["month_list"] = [nxt] + months
                st["month_keys"] = {lenses.same_address_key(m) for m in st["month_list"]}
                st["month_upto"] = 1
                st["origin"][nxt] = "month"
                queue_months(st, 1 + config.MONTH_LOOKAHEAD)
    for r in runs_of(data):
        add_run(result, r, "listing")
        hunt(result, r)
    if data.get("notes"):
        result["notes"].append(data["notes"])


def queue_months(st: dict, upto: int) -> None:
    """Queue the calendar's months up to position `upto` (the months of a calendar read with a look-ahead)."""
    lst = st["month_list"]
    upto = min(upto, len(lst))
    for m in lst[st["month_upto"]:upto]:
        if m not in st["visited"] and m not in st["queue"]:
            st["queue"].append(m)
            st["origin"][m] = "month"
    st["month_upto"] = max(st["month_upto"], upto)


def _light_listing(snap):
    """What a schedule page needs to keep while its answer is pending: its address and text (for the times shown)."""
    return SimpleNamespace(og_image=snap.og_image, final_url=snap.final_url, text=snap.text)


def open_listings(br: Browser, ex: Extractor, result: dict, use_cache: bool = True, batch: Batch | None = None,
                  stop_at: float | None = None) -> None:
    """The further schedule pages (the dance programme, other categories, next pages, the months of a calendar).
    With a batch they go out at half price and come back through apply_batch, which may queue more; without one
    they are read straight away. Months have their own allowance (MONTHS_AHEAD), the rest share the listing cap."""
    source, st = result["source"], result["_state"]
    max_listing = config.MAX_LISTING_PAGES_CORE if st["core"] else config.MAX_LISTING_PAGES
    t0 = time.monotonic()
    try:
        while st["queue"]:
            if stop_at and time.time() > stop_at:
                if not st["out_of_time"]:
                    st["out_of_time"] = True
                    result["notes"].append(f"out of time after {result['pages']} pages; the rest next run")
                return
            url = st["queue"].pop(0)
            if url in st["visited"]:
                continue
            month = st["origin"].get(url) == "month"
            if not month and st["listing_opened"] >= max_listing:
                st["listing_skipped"] = st.get("listing_skipped", 0) + 1
                continue
            st["visited"].add(url)
            if not month:
                st["listing_opened"] += 1
            snap = open_page(br, result, url)
            if not snap:
                continue
            st["readable"] += 1
            if batch is None:
                handle_listing(result, snap, read_page(ex, snap, source, st["venues"], LISTING, use_cache), url)
                continue
            h = page_hash(snap)
            hit = db.cache_get(snap.final_url, LISTING, h, ex.model) if use_cache else None
            if hit is not None:
                ex.cache_hits += 1
                handle_listing(result, snap, hit, url)
                continue
            batch.add(ex.request_params(snap, source, st["venues"], LISTING), ("more", result, _light_listing(snap), url, h))
            result["pending"] += 1
    finally:
        _spent(result, t0)


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
                         "cap": config.DETAIL_CAP.get(2 if prio is None else prio, config.MAX_DETAIL_PAGES),
                         "core": prio in config.CORE_TIERS, "origin": {}, "learned": [], "listing_dates": [],
                         "paged": False, "months_queued": False, "runs": {}, "hunt": {}, "hunt_queue": [],
                         "hunts_used": 0, "listing_opened": 0, "month_list": [], "month_upto": 0, "months_read": 0,
                         "months_empty": 0, "first_waiting": False, "left_open": 0}}
    st = result["_state"]
    t0 = time.monotonic()
    start = source.get("schedule_url") or source.get("website")
    st["visited"].add(start)
    for u in (source.get("extra_schedule_urls") or [])[: config.MAX_EXTRA_START_PAGES]:
        u = lenses.month_now(u, st["today"]) if u else u        # a remembered month page that has passed -> this month
        if u and u != start and u not in st["queue"]:
            st["queue"].append(u)                         # other dance categories / calendars learned before
            st["origin"][u] = "extra"
    snap = open_page(br, result, start)
    st["listing_opened"] += 1
    if snap and st["core"] and hasattr(br, "sitemap"):
        try:
            snap.sitemap = br.sitemap(snap.final_url, config.SITEMAP_LIMIT)
        except Exception:
            snap.sitemap = []
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
            st["first_waiting"] = True
        else:
            st["first"] = (snap, read_page(ex, snap, source, st["venues"], LISTING, use_cache))
    _spent(result, t0)
    return result


def _spent(result: dict, t0: float) -> None:
    st = result["_state"]
    st["browse_s"] += time.monotonic() - t0
    result["minutes"] = round(st["browse_s"] / 60, 1)


def continue_source(br: Browser, ex: Extractor, result: dict, use_cache: bool = True, batch: Batch | None = None,
                    max_minutes: float | None = None, stop_at: float | None = None,
                    listing_batch: Batch | None = None) -> dict:
    """Everything after the first listing page: more listing pages, then the production pages. Called again each
    time answers come back (it only opens what has not been opened yet).
    batch: production pages; listing_batch: the further schedule pages (None = read them straight away).
    max_minutes: browsing time allowed for this source; stop_at: clock time after which no page is opened."""
    t0 = time.monotonic()
    source, st = result["source"], result["_state"]
    if max_minutes is not None:
        st["max_s"] = max_minutes * 60
    if st["first"]:
        snap, data = st["first"]
        if data is None:          # the request failed in the batch (not billed): read it now
            data = read_page(ex, snap, source, st["venues"], LISTING, use_cache)
        st["first"] = None
        handle_listing(result, snap, data, source.get("schedule_url") or source.get("website"))
    _spent(result, t0)
    open_listings(br, ex, result, use_cache, listing_batch, stop_at)
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
            todo = [u for u in st["details"][: st["cap"]] + st["hunt_queue"] if u not in st["visited"]]
            if not todo:
                return
            for url in todo:
                if use_cache and url not in st["hunt"]:
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
    why = []                                   # why a site is only partly read, kept at the front of its notes
    if len(details) > cap:
        why.append(f"only {cap} of {len(details)} production pages read")
    if st["out_of_time"]:
        why.append("out of time")
    if result["blocked"]:
        why.append("some pages blocked (" + ", ".join(sorted(set(result["blocked"]))) + ")")
    if st.get("left_open"):
        why.append(f"{st['left_open']} pages still with Claude when the run ended (picked up at the next run)")
    elif st.get("unanswered"):
        why.append("some pages not answered by Claude in time")
    result["why"] = "; ".join(why)
    if result["items"]:
        incomplete = (len(details) > cap or st["out_of_time"] or result["blocked"] or st.get("unanswered")
                      or st.get("left_open"))
        result["status"] = "partial" if incomplete else "ok"
    elif st.get("left_open"):
        result["status"] = "partial"            # its pages are still with Claude: nothing to judge yet
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
    result["runs"] = settle_runs(result)
    result["coverage"] = coverage_checks(result)
    at = 1 if result["check"] else 0             # the "found far fewer" warning stays first
    for c in reversed(result["coverage"]):
        result["notes"].insert(at, c)
    return result


def _same_place(a: str | None, b: str | None) -> bool:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    return not a or not b or a == b or a in b or b in a


def settle_runs(result: dict) -> list[dict]:
    """How complete each run with missing dates is now, after all lenses: complete, partial or range_only."""
    out = []
    for r in result["_state"]["runs"].values():
        title, work = (r.get("title") or "").lower(), (r.get("work") or "").lower()
        dates = {(it["date"], it["time"]) for it in result["items"]
                 if ((it["title"] or "").lower() == title or (work and (it["work"] or "").lower() == work))
                 and _same_place(it["venue"], r.get("venue"))
                 and (not r.get("start") or it["date"] >= r["start"]) and (not r.get("end") or it["date"] <= r["end"])}
        via_hunt = any(it.get("date_source") in ("booking", "venue_site") for it in result["items"]
                       if (it["title"] or "").lower() == title and _same_place(it["venue"], r.get("venue")))
        exp, found = r.get("expected"), len(dates)
        if exp and found >= exp:
            status = "complete"
        elif found <= 1:
            status = "range_only"
        elif not exp and via_hunt and found >= 2:
            status = "complete"
        else:
            status = "partial"
        out.append({**r, "found": found, "status": status})
    return out


def coverage_checks(result: dict) -> list[str]:
    """Signs that part of a site's programme was not reached."""
    st, source, out = result["_state"], result["source"], []
    ld = sorted(st["listing_dates"])
    if len(ld) >= 3 and not st["paged"] and not st["details"]:
        span = (dt.date.fromisoformat(ld[-1]) - dt.date.fromisoformat(ld[0])).days
        if span <= 35:
            out.append(f"CHECK: the schedule showed only {ld[0]} to {ld[-1]}; the calendar may show one month at a time")
    if st["core"] and result["items"] and re.search(r"(opera|ballet|theatre|theater)", source.get("kind") or "", re.I):
        last = max(it["date"] for it in result["items"])
        if dt.date.fromisoformat(last) < st["today"] + dt.timedelta(days=75):
            if st.get("months_empty") and st.get("month_list"):
                # the calendar's later months were read and were empty: the house has not published further yet
                out.append(f"published so far up to {last} (the calendar's later months are still empty)")
            else:
                out.append(f"CHECK: dates found only up to {last}; the rest of the season may not have been reached")
    return out


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
        role = DETAIL if kind == "detail" else LISTING
        key = url if kind == "detail" else snap.final_url
        if data is not None and not data.get("_truncated"):
            db.cache_put(key, role, h, ex.model, data)
        if kind == "listing":
            result["_state"]["first"] = (snap, data)        # None: read it straight away in continue_source
            result["_state"]["first_waiting"] = False
            continue
        if data is None:
            result["_state"]["unanswered"] = True
            result["notes"].append(f"{url}: no answer from the batch; next run")
            continue
        if kind == "more":
            handle_listing(result, snap, data, url)
            result["_state"]["more"] = True                  # it may point to more pages: open them
            continue
        apply_detail(result, data, snap, url)
    return touched


MULTI_ZONE = {"United States", "USA", "Canada", "Australia", "Russia", "Brazil", "Mexico"}
IBERIA = {"Spain": "Europe/Madrid", "Portugal": "Europe/Lisbon"}


def _offset(tz: str, date: str | None):
    try:
        day = dt.datetime.fromisoformat((date or dt.date.today().isoformat()) + "T20:00")
        return day.replace(tzinfo=ZoneInfo(tz)).utcoffset()
    except Exception:
        return None


def _timezone(item: dict, source: dict) -> str | None:
    """The venue's time zone: as read with the date, else from its country when abroad, else the source's.
    A zone read with the date that does not fit the date's country is replaced by the country's zone (a Lisbon
    company's show in Madrid was read with Lisbon time, which put it an hour off)."""
    country = (item.get("country") or "").strip()
    by_country = config.TZ_BY_COUNTRY.get(country)
    own = item.get("timezone")
    if own:
        if country in IBERIA:
            # mainland Spain and Portugal differ by an hour; the islands keep their own zone
            return IBERIA[country] if own in IBERIA.values() else own
        if by_country and country not in MULTI_ZONE and _offset(own, item.get("date")) != _offset(by_country, item.get("date")):
            return by_country
        return own
    if country and country != source.get("country") and by_country:
        return by_country
    return source.get("timezone")


def recheck_days(source: dict, found: int, runs: list | None = None, today: dt.date | None = None) -> int:
    p = source.get("priority")
    days = config.RECHECK_DAYS.get(2 if p is None else p, 14)
    if source.get("kind") == "Festival" and found == 0:
        days = max(days, config.FESTIVAL_WAIT_DAYS)
    today = today or dt.date.today()
    soon = [r for r in (runs or []) if r["status"] != "complete" and r.get("start")
            and dt.date.fromisoformat(r["start"]) <= today + dt.timedelta(days=60)]
    if soon and p in config.CORE_TIERS:
        days = min(days, 7)                     # dates of a run opening soon are usually published in the last weeks
    return days


def _is_venue(source: dict) -> bool:
    return not re.search(r"(company|festival|competition)", source.get("kind") or "", re.I)


def _ticket(it: dict) -> str | None:
    """The booking link, never a reseller or listing site: then the production page on the venue's own site."""
    url = it.get("ticket_url")
    if url and lenses.is_aggregator(url):
        detail = it.get("detail_url")
        return detail if detail and not lenses.is_aggregator(detail) else None
    return url


def to_staging(result: dict, run_date: str) -> list[dict]:
    s = result["source"]
    host = urlparse(s.get("website") or "").netloc
    venue_source = _is_venue(s)
    rows = []
    for it in result["items"]:
        # a venue's own shows may leave out the city; a company's or festival's never borrow its home city
        home = venue_source and not it["venue"]
        rows.append({
            "source_id": s["id"], "source": host,
            "title": it["title"], "original_title": it["original_title"],
            "work": it["work"], "work_is_story": it["work_is_story"],
            "choreographer": it["choreographer"], "company": it["company"],
            "venue": it["venue"],
            "city": it["city"] or (s.get("city") if home or (venue_source and not it["country"]) else None),
            "country": it["country"] or (s.get("country") if venue_source or not it["city"] else None),
            "timezone": _timezone(it, s),
            "performance_date": it["date"], "performance_time": it["time"], "time_tbc": it["time"] is None,
            "tags": it["tags"], "genre": it["tags"][0],
            "program": it["program"], "program_position": it["program_position"],
            "tickets_url": _ticket(it), "image_url": it["image_url"], "description": it["description"],
            "cancelled": it["cancelled"], "date_source": it.get("date_source") or "listed",
            "dedupe_key": db.dedupe_key(host, it, run_date), "status": "pending",
        })
    return rows


def run_rows(result: dict) -> list[dict]:
    s = result["source"]
    rows = []
    for r in result.get("runs") or []:
        key = hashlib.md5("|".join([s["id"], (r.get("title") or "").lower(), (r.get("venue") or "").lower(),
                                    r.get("start") or ""]).encode()).hexdigest()
        rows.append({"run_key": key, "source_id": s["id"], "detail_url": r.get("detail_url"), "title": r["title"],
                     "work": r.get("work"), "company": r.get("company"), "choreographer": r.get("choreographer"),
                     "venue": r.get("venue"), "city": r.get("city"), "country": r.get("country"),
                     "run_start": r.get("start"), "run_end": r.get("end"), "expected_count": r.get("expected"),
                     "found_count": r["found"], "status": r["status"], "lenses_tried": r.get("lenses") or [],
                     "dates_page_url": r.get("dates_page_url"), "image_url": r.get("image_url"),
                     "description": r.get("description")})
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
        harvest_pending(ex)
    use_batch = config.USE_BATCH and not args.no_batch
    listings, details = (Batch(ex), Batch(ex)) if use_batch else (None, None)
    job_end = started + config.JOB_MINUTES * 60
    stop_pages = job_end - config.DETAIL_RESERVE_MINUTES * 60     # no page is opened after this
    report, results, skipped, waiting = [], [], [], []
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

    def advance(res):
        """Answers came back for this source: open whatever new pages they point to."""
        st = res["_state"]
        if res.get("saved") or not (st["more"] or st["first"]):
            return
        st["more"] = False
        try:
            # with the credit gone nothing new is opened, but an answer that came back is still used
            continue_source(br, ex, res, use_cache, details,
                            stop_at=time.time() if ex.out_of_credit else stop_pages, listing_batch=details)
        except OutOfCredit:
            st["unanswered"] = True
        except Exception:
            traceback.print_exc()

    def handle(batch, finished):
        """Answers came back: apply them, open any new pages they point to, save sources that are done."""
        for res in apply_batch(batch, finished, ex):
            advance(res)
            if not res["pending"]:
                finish(res)

    batches = [b for b in (listings, details) if b]
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
        try:
            if listings and listings.requests:
                # wait for the schedule pages (up to LISTING_BATCH_MINUTES); any still running stay open and are
                # picked up in the loops below as they finish
                apply_batch(listings, listings.run(deadline=min(started + config.LISTING_BATCH_MINUTES * 60, job_end)), ex)

            # Round 2: the further schedule pages and the production pages, all at half price through the batch.
            # They go out in bundles while browsing continues; answers that point to more pages open them in turn,
            # and every source is saved as soon as its own pages are answered.
            last_sent = time.time()
            for res in results:
                s, st = res["source"], res["_state"]
                if st["first_waiting"] or res.get("saved"):
                    continue                         # its schedule page is still with Claude: handled when it returns
                if ex.out_of_credit and st["first"] and st["first"][1] is None:
                    skipped.append(s)                # schedule page never read: leave the source due, untouched
                    res["saved"] = True
                    continue
                print(f"=> {s['name']}", flush=True)
                try:
                    continue_source(br, ex, res, use_cache, details,
                                    stop_at=time.time() if ex.out_of_credit else stop_pages, listing_batch=details)
                except OutOfCredit:
                    st["unanswered"] = True
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
                    for b in batches:
                        handle(b, b.poll())

            # Wait for the last answers (they may open more pages), saving sources as they complete.
            while any(b.busy for b in batches) and time.time() < job_end:
                if details and details.unsent:
                    details.submit()
                got = False
                for b in batches:
                    finished = b.poll(force=True)
                    if finished:
                        got = True
                        handle(b, finished)
                if not got and any(b.busy for b in batches):
                    time.sleep(Batch.POLL_S)
        finally:
            # Anything still with Claude stays there (it is paid for): remember it, the next run picks it up.
            left_open(batches, ex)
        for res in results:
            st = res["_state"]
            if not res.get("saved") and st["first"] and st["first"][1] is None:
                res["saved"] = True                  # its schedule page never came back: leave the source due, untouched
                waiting.append(res["source"])
                continue
            finish(res)

    for s in waiting:
        report.append({"source": s["name"], "status": "not read", "performances": 0, "staged": 0, "pages": 0,
                       "reused": 0, "check": False, "blocked": [], "cookies": [], "screens": [], "minutes": 0,
                       "notes": ["its schedule page was still with Claude when the run ended; it stays due and the "
                                 "answer is picked up at the next run"]})
    for s in skipped:
        report.append({"source": s["name"], "status": "not read", "performances": 0, "staged": 0, "pages": 0,
                       "reused": 0, "check": False, "blocked": [], "cookies": [], "screens": [], "minutes": 0,
                       "notes": ["the Anthropic credit ran out before this source; it stays due for the next run"]})
    write_report(report, ex, args, [b for b in (listings, details) if b], credit_out=ex.out_of_credit)
    return 0


def left_open(batches: list, ex: Extractor) -> None:
    """Pages still being answered when the run ends: their sources are saved with what they have (and looked at
    again next run), and the pages are recorded so the next run collects the answers instead of paying again."""
    rows = {}
    for b in batches:
        for key in b.drop_unsent():
            ctx = b.context.pop(key, None)
            if ctx is not None:
                ctx[1]["pending"] -= 1
                ctx[1]["_state"]["unanswered"] = True
        for bid, keys in b.leave_open().items():
            for key in keys:
                ctx = b.context.pop(key, None)
                if ctx is None:
                    continue
                kind, result, snap, url, h = ctx
                result["pending"] -= 1
                st = result["_state"]
                st["left_open"] = st.get("left_open", 0) + 1
                st["first_waiting"] = False
                row = {"url": url if kind == "detail" else snap.final_url, "role": DETAIL if kind == "detail" else LISTING,
                       "text_hash": h, "model": ex.model, "batch_id": bid, "custom_id": key}
                rows[(row["url"], row["role"])] = row          # one row per page (the table's key)
    if rows:
        print(f"{len(rows)} pages still with Claude at the end of the run: kept for the next run", flush=True)
        db.pending_put(list(rows.values()))


def harvest_pending(ex: Extractor) -> int:
    """Collect the answers to pages left with the Batch API by earlier runs and store them as readings, so this
    run reuses them for free when the page has not changed. Returns how many readings were stored."""
    rows = db.pending_all()
    if not rows:
        return 0
    by_batch: dict[str, list[dict]] = {}
    for r in rows:
        by_batch.setdefault(r["batch_id"] or "", []).append(r)
    stored = 0
    now = dt.datetime.now(dt.timezone.utc)
    for bid, items in by_batch.items():
        try:
            age = max((now - dt.datetime.fromisoformat(r["updated_at"])).days for r in items if r.get("updated_at"))
        except Exception:
            age = 0
        got = None
        try:
            if bid and ex.client.messages.batches.retrieve(bid).processing_status != "ended":
                if age < 30:
                    continue                          # still running: next time
            elif bid:
                got = {e.custom_id: e for e in ex.client.messages.batches.results(bid)}
        except Exception as e:
            gone = any(w in str(e).lower() for w in ("not_found", "not found", "404", "expired"))
            if not gone and age < 30:
                print(f"pending batch {bid}: {type(e).__name__}; trying again next run", flush=True)
                continue
        readings = []
        for r in items:
            entry = (got or {}).get(r["custom_id"])
            if entry is not None and entry.result.type == "succeeded":
                data = ex.parse(entry.result.message)
                if not data.get("_truncated"):
                    readings.append((r["url"], r["role"], r["text_hash"], r["model"], data))
        for reading in readings:
            db.cache_put(*reading)
        stored += len(readings)
        for r in items:
            db.pending_done(r["url"], r["role"])
    if stored:
        print(f"{stored} answers left by earlier runs collected (reused for free when the page is unchanged)", flush=True)
    return stored


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
    days = 0 if res.get("retry_soon") or res["_state"].get("left_open") or res["_state"].get("unanswered") else \
        recheck_days(s, len(rows), res.get("runs"), res["_state"]["today"])
    fields = {
        "status": res["status"],
        "next_check_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(),
        "notes": ((s.get("notes") or "").split(" || last run:")[0]
                  + f" || last run: {run_date}, {len(rows)} performances, {res['pages']} pages"
                  + (f", partial because: {res['why']}" if res["status"] == "partial" and res.get("why") else "")
                  + (f", {'; '.join(res['notes'])[:300]}" if res["notes"] else "")),
    }
    if res["schedule_url"] and not s.get("schedule_url"):
        fields["schedule_url"] = res["schedule_url"]
    learned = [u for u in res["_state"]["learned"] if u not in (s.get("extra_schedule_urls") or [])]
    if learned:
        fields["extra_schedule_urls"] = ((s.get("extra_schedule_urls") or []) + learned)[: config.MAX_EXTRA_START_PAGES]
    runs = run_rows(res)
    if runs and not args.dry:
        db.upsert_runs(runs)
    if not s.get("read_method"):
        fields["read_method"] = res["read_method"]
    if not args.dry:
        db.update_source(s["id"], fields)
    entry = {"source": s["name"], "status": res["status"], "performances": len(rows), "staged": staged,
             "pages": res["pages"], "reused": res.get("reused", 0), "check": res["check"],
             "upcoming_before": res.get("upcoming_before"), "blocked": sorted(set(res["blocked"])),
             "cookies": sorted(set(res["cookies"])), "screens": res["screens"][:5],
             "minutes": res.get("minutes", 0),
             "notes": ([f"PARTIAL because: {res['why']}"] if res["status"] == "partial" and res.get("why") else [])
                      + res["notes"][:4],
             "coverage": res.get("coverage", []),
             "runs": [{"title": r["title"], "venue": r.get("venue"), "start": r.get("start"), "end": r.get("end"),
                       "found": r["found"], "expected": r.get("expected"), "status": r["status"],
                       "lenses": r.get("lenses") or []} for r in res.get("runs") or []],
             "learned": learned}
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
    problems = [r for r in report if r["status"] in ("blocked", "failed") or r["check"] or r.get("coverage")]
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
    open_runs = [(r["source"], x) for r in report for x in r.get("runs") or [] if x["status"] != "complete"]
    solved = sum(1 for r in report for x in r.get("runs") or [] if x["status"] == "complete")
    if open_runs or solved:
        lines += ["### Date hunt", f"{solved} runs completed by the date hunt; {len(open_runs)} still without all dates "
                  "(shown on the site as a date range until the dates appear).", "",
                  "| Source | Production | Venue | Run | Dates found | Looked at |", "|---|---|---|---|---|---|"]
        for src, x in open_runs[:60]:
            found = f"{x['found']} of {x['expected']}" if x.get("expected") else str(x["found"])
            lines.append(f"| {src} | {x['title']} | {x.get('venue') or '?'} | {x.get('start') or '?'} to {x.get('end') or '?'} | "
                         f"{found} | {', '.join(x['lenses']) or '-'} |")
        lines.append("")
    learned = [(r["source"], u) for r in report for u in r.get("learned") or []]
    if learned:
        lines += ["### New start pages learned", "Dance categories or calendars found this run that will be read every time from now on:", ""]
        lines += [f"- {src}: {u}" for src, u in learned] + [""]
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
