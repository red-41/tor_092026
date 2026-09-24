"""Collect upcoming dance performances from due sources and put them into the staging table.

Usage:
  python -m collector.run --limit 60                 # next 60 due sources
  python -m collector.run --priority 1 --limit 200   # only top-priority sources
  python -m collector.run --name "Sadler"            # one source by name
  python -m collector.run --shard 2 --shards 6       # one of 6 parallel slices (GitHub matrix)
  python -m collector.run --dry                      # read and report, write nothing
"""
import argparse
import datetime as dt
import json
import os
import sys
import traceback
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import config, db
from .browse import Browser
from .extract import Extractor, clean_performance, flatten, merge


def collect_source(br: Browser, ex: Extractor, source: dict) -> dict:
    """Read one source. Returns a result dict with performances and what happened."""
    tz = ZoneInfo(source.get("timezone") or "Europe/Paris")
    today = dt.datetime.now(tz).date()
    venues = db.known_venues(source.get("country"))
    result = {"source": source, "items": [], "pages": 0, "status": "ok", "notes": [], "schedule_url": None}

    start = source.get("schedule_url") or source.get("website")
    visited, queue, details = set(), [start], []
    statuses = []

    # 1. listing pages (the schedule, its pagination or next months)
    while queue and result["pages"] < config.MAX_LISTING_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        snap = br.load(url)
        result["pages"] += 1
        if snap.error:
            result["notes"].append(f"{url}: {snap.error}")
            continue
        data = ex.read(snap, source, venues, role="schedule or listing page")
        statuses.append(data.get("page_status"))
        if data.get("page_status") == "not_a_schedule" and data.get("schedule_url_guess") and result["pages"] == 1:
            guess = data["schedule_url_guess"]
            if guess not in visited:
                result["schedule_url"] = guess
                queue.insert(0, guess)
            continue
        if result["pages"] == 1 and not source.get("schedule_url"):
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
        visited.add(url)
        snap = br.load(url)
        result["pages"] += 1
        if snap.error:
            result["notes"].append(f"{url}: {snap.error}")
            continue
        data = ex.read(snap, source, venues, role="production page with dates and cast")
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
        result["status"] = "partial" if len(details) > config.MAX_DETAIL_PAGES else "ok"
    elif "not_published_yet" in statuses:
        result["status"] = "waiting"
    elif statuses and all(s in ("no_dance_programme", "not_a_schedule") for s in statuses):
        result["status"] = "no_dance"
    else:
        result["status"] = "failed"
    result["read_method"] = "detail_pages" if details else ("paged_list" if result["pages"] > 1 else "single_page")
    return result


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
            "timezone": s.get("timezone"),
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
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--priority", type=int)
    ap.add_argument("--name")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--model")
    ap.add_argument("--dry", action="store_true", help="read and report only; write nothing to Supabase")
    args = ap.parse_args(argv)

    run_date = dt.date.today().isoformat()
    sources = db.due_sources(args.limit, args.priority, args.name, args.shard, args.shards)
    ex = Extractor(args.model)
    report = []
    print(f"{len(sources)} sources due (shard {args.shard + 1}/{args.shards})", flush=True)

    with Browser() as br:
        for s in sources:
            print(f"-> {s['name']}", flush=True)
            try:
                res = collect_source(br, ex, s)
                rows = to_staging(res, run_date)
                staged = 0 if args.dry else db.stage(rows)
                fields = {
                    "status": res["status"],
                    "next_check_at": (dt.datetime.now(dt.timezone.utc)
                                      + dt.timedelta(days=config.RECHECK_DAYS.get(s.get("priority") or 2, 14))).isoformat(),
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
                report.append({"source": s["name"], "status": res["status"], "performances": len(rows),
                               "staged": staged, "pages": res["pages"], "notes": res["notes"][:3]})
            except Exception as e:
                traceback.print_exc()
                if not args.dry:
                    db.update_source(s["id"], {"status": "failed", "notes": (s.get("notes") or "")[:500]
                                               + f" || last run: {run_date}, error {type(e).__name__}"})
                report.append({"source": s["name"], "status": "failed", "performances": 0, "staged": 0,
                               "pages": 0, "notes": [f"{type(e).__name__}: {str(e)[:200]}"]})

    write_report(report, ex, args)
    return 0


def write_report(report, ex, args):
    ok = sum(1 for r in report if r["status"] in ("ok", "partial"))
    total = sum(r["performances"] for r in report)
    lines = [f"## Saffitt collector, shard {args.shard + 1}/{args.shards}",
             f"{len(report)} sources read, {ok} with performances, {total} performances staged.",
             f"Tokens: {ex.input_tokens:,} in, {ex.output_tokens:,} out ({ex.model}).", "",
             "| Source | Status | Performances | Pages | Notes |", "|---|---|---|---|---|"]
    for r in report:
        notes = "; ".join(r["notes"]).replace("|", "/")[:200]
        lines.append(f"| {r['source']} | {r['status']} | {r['performances']} | {r['pages']} | {notes} |")
    md = "\n".join(lines)
    print(md)
    os.makedirs("reports", exist_ok=True)
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
