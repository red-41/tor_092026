"""Collect upcoming dance performances from due sources and put them into the staging table.

Usage:
  python -m collector.run --limit 60                 # next 60 due sources
  python -m collector.run --priority 0 --limit 200   # only the tier 0 core
  python -m collector.run --name "Sadler"            # one source by name
  python -m collector.run --shard 2 --shards 12      # one of 12 parallel slices (GitHub matrix)
  python -m collector.run --status blocked           # retry every blocked source (from a home computer)
  python -m collector.run --dry                      # read and report, write nothing to the site's tables
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

from . import config, db
from .browse import Browser
from .extract import Extractor, clean_performance, flatten, merge, page_hash

THIN = 150          # characters: below this a page has nothing to read


def read_page(ex: Extractor, snap, source: dict, venues: list, role: str, use_cache: bool) -> dict:
    """Claude's reading of a page, reusing last time's reading when the page has not changed."""
    h = page_hash(snap)
    if use_cache:
        hit = db.cache_get(snap.final_url, role, h, ex.model)
        if hit is not None:
            ex.cache_hits += 1
            return hit
    data = ex.read(snap, source, venues, role=role)
    if not data.get("_truncated"):
        db.cache_put(snap.final_url, role, h, ex.model, data)
    return data


def _skip_reason(snap) -> str:
    if snap.error:
        return snap.error
    if snap.blocked:
        return f"blocked ({snap.blocked})"
    if len(snap.text) < THIN and not snap.events:
        return f"empty page ({len(snap.text)} characters)"
    return ""


def collect_source(br: Browser, ex: Extractor, source: dict, use_cache: bool = True,
                   max_minutes: float | None = None) -> dict:
    """Read one source. Returns a result dict with performances and what happened."""
    t0 = time.monotonic()
    max_minutes = max_minutes or config.MAX_SOURCE_MINUTES
    tz = ZoneInfo(source.get("timezone") or "Europe/Paris")
    today = dt.datetime.now(tz).date()
    venues = db.known_venues(source.get("country"))
    result = {"source": source, "items": [], "pages": 0, "status": "ok", "notes": [], "schedule_url": None,
              "blocked": [], "screens": [], "cookies": [], "check": False}

    start = source.get("schedule_url") or source.get("website")
    visited, queue, details = set(), [start], []
    statuses, readable, out_of_time = [], 0, False

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

    # 1. listing pages (the schedule, its pagination or next months)
    while queue and result["pages"] < config.MAX_LISTING_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        snap = open_page(url)
        if not snap:
            continue
        readable += 1
        data = read_page(ex, snap, source, venues, "schedule or listing page", use_cache)
        statuses.append(data.get("page_status"))
        if data.get("page_status") == "not_a_schedule" and data.get("schedule_url_guess") and readable == 1:
            guess = data["schedule_url_guess"]
            if guess not in visited:
                result["schedule_url"] = guess
                queue.insert(0, guess)
            continue
        if readable == 1 and not source.get("schedule_url"):
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
    for url in details[: config.MAX_DETAIL_PAGES]:
        if url in visited:
            continue
        if time.monotonic() - t0 > max_minutes * 60:
            out_of_time = True
            result["notes"].append(f"stopped after {max_minutes:.0f} minutes; the rest next run")
            break
        visited.add(url)
        snap = open_page(url)
        if not snap:
            continue
        readable += 1
        data = read_page(ex, snap, source, venues, "production page with dates and cast", use_cache)
        if data.get("notes"):
            result["notes"].append(data["notes"])
        for p in flatten(data):
            c = clean_performance(p, today)
            if c:
                c["image_url"] = c["image_url"] or snap.og_image or None
                c["ticket_url"] = c["ticket_url"] or snap.final_url
                result["items"].append(c)

    result["items"] = merge(result["items"])
    if len(details) > config.MAX_DETAIL_PAGES:
        result["notes"].append(f"only {config.MAX_DETAIL_PAGES} of {len(details)} production pages read")

    if result["items"]:
        incomplete = len(details) > config.MAX_DETAIL_PAGES or out_of_time or result["blocked"]
        result["status"] = "partial" if incomplete else "ok"
    elif result["blocked"] and not readable:
        result["status"] = "blocked"
    elif "not_published_yet" in statuses:
        result["status"] = "waiting"
    elif statuses and all(s in ("no_dance_programme", "not_a_schedule") for s in statuses):
        result["status"] = "no_dance"
    else:
        result["status"] = "failed"

    # 3. sanity check against what is already on Saffitt from this source (nothing is ever removed)
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--priority", type=int)
    ap.add_argument("--name")
    ap.add_argument("--status", help="every source in this status, due or not (e.g. blocked)")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--model")
    ap.add_argument("--no-cache", action="store_true", help="send every page to Claude even if unchanged")
    ap.add_argument("--dry", action="store_true", help="read and report only; write nothing to staging or sources")
    args = ap.parse_args(argv)

    run_date = dt.date.today().isoformat()
    sources = db.due_sources(args.limit, args.priority, args.name, args.shard, args.shards, status=args.status)
    ex = Extractor(args.model)
    if sources:
        ex.check()
    report = []
    os.makedirs("reports", exist_ok=True)
    progress = f"reports/progress-shard{args.shard}.jsonl"
    print(f"{len(sources)} sources due (shard {args.shard + 1}/{args.shards})", flush=True)

    with Browser(shots_dir="reports/screens", shots="problems") as br:
        for s in sources:
            print(f"-> {s['name']}", flush=True)
            t0 = time.monotonic()
            try:
                res = collect_source(br, ex, s, use_cache=not args.no_cache)
                rows = to_staging(res, run_date)
                staged = 0 if args.dry else db.stage(rows)
                fields = {
                    "status": res["status"],
                    "next_check_at": (dt.datetime.now(dt.timezone.utc)
                                      + dt.timedelta(days=recheck_days(s, len(rows)))).isoformat(),
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
                entry = {"source": s["name"], "status": res["status"], "performances": len(rows),
                         "staged": staged, "pages": res["pages"], "check": res["check"],
                         "upcoming_before": res.get("upcoming_before"), "blocked": sorted(set(res["blocked"])),
                         "cookies": sorted(set(res["cookies"])), "screens": res["screens"][:5],
                         "minutes": round((time.monotonic() - t0) / 60, 1), "notes": res["notes"][:4]}
            except Exception as e:
                traceback.print_exc()
                if not args.dry:
                    try:
                        db.update_source(s["id"], {"status": "failed", "notes": (s.get("notes") or "").split(" || last run:")[0]
                                                   + f" || last run: {run_date}, error {type(e).__name__}"})
                    except Exception:
                        pass
                entry = {"source": s["name"], "status": "failed", "performances": 0, "staged": 0, "pages": 0,
                         "check": False, "blocked": [], "cookies": [], "screens": [], "minutes": 0,
                         "notes": [f"{type(e).__name__}: {str(e)[:200]}"]}
            report.append(entry)
            db.record_check({
                "kind": "collect", "source_id": s["id"], "source_name": s["name"],
                "url": s.get("schedule_url") or s.get("website"), "verdict": entry["status"],
                "blocked_by": ", ".join(entry["blocked"]) or None, "cookie_banner": ", ".join(entry["cookies"]) or None,
                "performances": entry["performances"], "pages": entry["pages"], "upcoming_before": entry.get("upcoming_before"),
                "needs_check": bool(entry["check"]), "minutes": entry["minutes"],
                "notes": "; ".join(entry["notes"])[:2000] or None,
                "screenshot": ", ".join(os.path.basename(x) for x in entry["screens"]) or None})
            with open(progress, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    write_report(report, ex, args)
    return 0


def write_report(report, ex, args):
    by = {}
    for r in report:
        by[r["status"]] = by.get(r["status"], 0) + 1
    total = sum(r["performances"] for r in report)
    problems = [r for r in report if r["status"] in ("blocked", "failed") or r["check"]]
    lines = [f"## Saffitt collector, shard {args.shard + 1}/{args.shards}" + (" (dry run)" if args.dry else ""),
             f"{len(report)} sources: " + ", ".join(f"{v} {k}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
             + f". {total} performances found.",
             f"Claude: {ex.calls} pages read, {ex.cache_hits} unchanged pages reused, "
             f"{ex.input_tokens:,} tokens in, {ex.output_tokens:,} out ({ex.model}).", ""]
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
