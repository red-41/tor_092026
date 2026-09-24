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
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import config
import db
from browse import Browser
from extract import Batch, Extractor, clean_performance, flatten, merge, page_hash

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
    """Add the performances from one production page to a source's result."""
    today = result["_state"]["today"]
    if data.get("notes"):
        result["notes"].append(data["notes"])
    for p in flatten(data):
        c = clean_performance(p, today)
        if c:
            c["image_url"] = c["image_url"] or (snap.og_image if snap else None) or None
            c["ticket_url"] = c["ticket_url"] or (snap.final_url if snap else url)
            result["items"].append(c)


def collect_source(br: Browser, ex: Extractor, source: dict, use_cache: bool = True,
                   max_minutes: float | None = None, batch: Batch | None = None) -> dict:
    """Read one source. Production pages are answered now, or later through the batch.
    Call finalize() once every page has an answer (done here when nothing is left for the batch)."""
    t0 = time.monotonic()
    max_minutes = max_minutes or config.MAX_SOURCE_MINUTES
    tz = ZoneInfo(source.get("timezone") or "Europe/Paris")
    today = dt.datetime.now(tz).date()
    venues = db.known_venues(source.get("country"))
    prio = source.get("priority")
    cap = config.DETAIL_CAP.get(2 if prio is None else prio, config.MAX_DETAIL_PAGES)
    result = {"source": source, "items": [], "pages": 0, "status": "ok", "notes": [], "schedule_url": None,
              "blocked": [], "screens": [], "cookies": [], "check": False, "reused": 0, "pending": 0,
              "_state": {"today": today, "statuses": [], "readable": 0, "out_of_time": False, "details": [], "cap": cap}}
    st = result["_state"]

    start = source.get("schedule_url") or source.get("website")
    visited, queue, details = set(), [start], st["details"]

    def open_page(url):
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

    # 1. listing pages (the schedule, its pagination or next months): always answered straight away
    while queue and result["pages"] < config.MAX_LISTING_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        snap = open_page(url)
        if not snap:
            continue
        st["readable"] += 1
        data = read_page(ex, snap, source, venues, LISTING, use_cache)
        st["statuses"].append(data.get("page_status"))
        if data.get("page_status") == "not_a_schedule" and data.get("schedule_url_guess") and st["readable"] == 1:
            guess = data["schedule_url_guess"]
            if guess not in visited:
                result["schedule_url"] = guess
                queue.insert(0, guess)
            continue
        if st["readable"] == 1 and not source.get("schedule_url"):
            result["schedule_url"] = snap.final_url
        for p in flatten(data):
            c = clean_performance(p, today)
            if c:
                result["items"].append(c)
        details += [u for u in data.get("detail_links", []) if u not in details]
        nxt = data.get("next_page_url")
        if nxt and nxt not in visited:
            queue.append(nxt)
        if data.get("notes"):
            result["notes"].append(data["notes"])

    # 2. production pages with the individual dates and times
    for url in details[:cap]:
        if url in visited:
            continue
        visited.add(url)
        if use_cache:
            prev = db.cache_peek(url, DETAIL)
            if reusable(prev, today):                  # read before, next show not soon: no need to open it
                result["reused"] += 1
                apply_detail(result, prev["result"], None, url)
                continue
        if time.monotonic() - t0 > max_minutes * 60:
            st["out_of_time"] = True
            result["notes"].append(f"stopped after {max_minutes:.0f} minutes; the rest next run")
            break
        snap = open_page(url)
        if not snap:
            continue
        st["readable"] += 1
        if batch is None:
            apply_detail(result, read_page(ex, snap, source, venues, DETAIL, use_cache, key=url), snap, url)
            continue
        h = page_hash(snap)
        hit = db.cache_get(url, DETAIL, h, ex.model) if use_cache else None
        if hit is not None:
            ex.cache_hits += 1
            apply_detail(result, hit, snap, url)
            continue
        batch.add(ex.request_params(snap, source, venues, DETAIL), (result, snap, url, h))
        result["pending"] += 1

    result["minutes"] = round((time.monotonic() - t0) / 60, 1)
    if not result["pending"]:
        finalize(result)
    return result


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


def apply_batch(batch: Batch, answers: dict, ex: Extractor) -> None:
    """Hand each batch answer back to its source."""
    for key, (result, snap, url, h) in batch.context.items():
        data = answers.get(key)
        result["pending"] -= 1
        if data is None:
            result["_state"]["unanswered"] = True
            result["notes"].append(f"{url}: no answer from the batch; next run")
            continue
        if not data.get("_truncated"):
            db.cache_put(url, DETAIL, h, ex.model, data)
        apply_detail(result, data, snap, url)


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
    ap.add_argument("--no-batch", action="store_true", help="answer production pages straight away (full price)")
    ap.add_argument("--dry", action="store_true", help="read and report only; write nothing to staging or sources")
    args = ap.parse_args(argv)

    run_date = dt.date.today().isoformat()
    sources = db.due_sources(args.limit, args.priority, args.name, args.shard, args.shards, status=args.status)
    ex = Extractor(args.model)
    if sources:
        ex.check()
    batch = Batch(ex) if (config.USE_BATCH and not args.no_batch) else None
    report = []
    os.makedirs("reports", exist_ok=True)
    print(f"{len(sources)} sources due (shard {args.shard + 1}/{args.shards})"
          + (", production pages via the Batch API" if batch else ""), flush=True)

    results = []
    with Browser(shots_dir="reports/screens", shots="problems") as br:
        for s in sources:
            print(f"-> {s['name']}", flush=True)
            try:
                res = collect_source(br, ex, s, use_cache=not args.no_cache, batch=batch)
                results.append(res)
                if not res["pending"]:
                    report.append(save(res, args, run_date))
            except Exception as e:
                traceback.print_exc()
                report.append(save_failure(s, e, args, run_date))

    if batch and batch.requests:
        answers = batch.run(deadline=started + config.JOB_MINUTES * 60)
        apply_batch(batch, answers, ex)
        for res in results:
            if "upcoming_before" in res:          # already finalised and saved above
                continue
            try:
                report.append(save(finalize(res), args, run_date))
            except Exception as e:
                traceback.print_exc()
                report.append(save_failure(res["source"], e, args, run_date))

    write_report(report, ex, args, batch)
    return 0


def save(res: dict, args, run_date: str) -> dict:
    """Stage the performances, update the source, and record the outcome."""
    s = res["source"]
    rows = to_staging(res, run_date)
    staged = 0 if args.dry else db.stage(rows)
    fields = {
        "status": res["status"],
        "next_check_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=recheck_days(s, len(rows)))).isoformat(),
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


def write_report(report, ex, args, batch=None):
    by = {}
    for r in report:
        by[r["status"]] = by.get(r["status"], 0) + 1
    total = sum(r["performances"] for r in report)
    problems = [r for r in report if r["status"] in ("blocked", "failed") or r["check"]]
    lines = [f"## Saffitt collector, shard {args.shard + 1}/{args.shards}" + (" (dry run)" if args.dry else ""),
             f"{len(report)} sources: " + ", ".join(f"{v} {k}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
             + f". {total} performances found.",
             f"Claude ({ex.model}): {ex.calls} pages read at full price ({ex.input_tokens:,} tokens in, "
             f"{ex.output_tokens:,} out)"
             + (f"; {batch.calls} pages via the Batch API at half price ({batch.input_tokens:,} in, {batch.output_tokens:,} out)"
                if batch else "")
             + f"; {ex.cache_hits} unchanged pages and {sum(r.get('reused', 0) for r in report)} production pages reused for free.", ""]
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
