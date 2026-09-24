"""Free check of every source before spending Claude tokens.

Opens each source's schedule page (or home page) exactly as the collector would and reports:
can the bot get in, was it blocked (and by what), what happened to the cookie banner,
is there readable content, does it mention dance and dates. Takes a screenshot of every page.
Uses no Claude tokens. Writes only its findings, to the source_checks table (nothing on the site changes).

  python preflight.py                     # all sources
  python preflight.py --shard 0 --shards 12
  python preflight.py --name "Opera"
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

import db
from browse import Browser

DANCE = re.compile(r"\b(ballet|ballett|balett|balet|baletu|balé|dance|dancers?|danse|danseurs?|tanz|tanztheater|dans|dansen|"
                   r"danza|dança|taniec|tance|tanec|tánc|tanssi|choreograph\w*|chorégraph\w*|choreograf\w*|coreograf\w*)\b", re.I)
MONTHS = (r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|januar|februar|märz|maerz|mai|juni|juli|okt|dez|"
          r"janvier|février|fevrier|mars|avril|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre|"
          r"januari|februari|maart|mei|augustus|gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|"
          r"ottobre|dicembre|enero|febrero|abril|mayo|junio|julio|septiembre|octubre|noviembre|diciembre|"
          r"styczni|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|wrze|październik|listopada|grudnia|"
          r"ledna|února|března|dubna|května|června|července|srpna|září|října|listopadu|prosince|"
          r"tammi|helmi|maalis|huhti|touko|kesä|heinä|elo|syys|loka|marras|joulu)")
DATES = re.compile(r"(\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{1,2}\.?\s+" + MONTHS + r"|" + MONTHS
                   + r"\w*\.?\s+\d{1,2}\b|\b20\d\d-\d\d-\d\d\b)", re.I)


def verdict(snap) -> str:
    if snap.error.startswith("blocked by robots.txt"):
        return "ROBOTS"
    if snap.error:
        return "ERROR"
    if snap.blocked:
        return "BLOCKED"
    if snap.status >= 400:
        return "ERROR"
    if len(snap.text) < 300 and not snap.events:
        return "EMPTY"
    if not DATES.search(snap.text) and not snap.events:
        return "NO DATES"
    return "OK"


EXPLAIN = {
    "OK": "opens fine, has text with dates",
    "NO DATES": "opens fine but no dates on this page (usually the home page; the collector will look for the schedule)",
    "EMPTY": "opens but almost no text (JavaScript app, consent wall, or nothing published)",
    "BLOCKED": "the site refused the bot (Cloudflare, captcha, 403...)",
    "ERROR": "could not open (timeout, bad address, 404, certificate)",
    "ROBOTS": "the site's robots.txt asks bots not to read this page",
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    args = ap.parse_args(argv)

    sources = db.all_sources(args.name, args.shard, args.shards)
    print(f"{len(sources)} sources (shard {args.shard + 1}/{args.shards})", flush=True)
    rows = []
    os.makedirs("reports", exist_ok=True)
    with Browser(shots_dir="reports/screens", shots="always") as br:
        for s in sources:
            url = s.get("schedule_url") or s.get("website")
            if not url:
                continue
            snap = br.load(url)
            v = verdict(snap)
            row = {
                "source": s["name"], "priority": s.get("priority"), "country": s.get("country"), "url": url,
                "verdict": v, "blocked_by": snap.blocked, "http": snap.status, "cookie_banner": snap.cookie,
                "text_chars": len(snap.text), "event_data": len(snap.events), "dates_seen": len(DATES.findall(snap.text)),
                "dance_words": len(DANCE.findall(snap.text)), "final_url": snap.final_url,
                "error": snap.error[:200], "screenshot": os.path.basename(snap.screenshot) if snap.screenshot else "",
                "source_id": s["id"],
            }
            rows.append(row)
            db.record_check({
                "kind": "preflight", "source_id": s["id"], "source_name": s["name"], "url": url,
                "final_url": snap.final_url, "verdict": v, "blocked_by": snap.blocked or None, "http_status": snap.status,
                "cookie_banner": snap.cookie, "text_chars": len(snap.text), "dates_seen": row["dates_seen"],
                "dance_words": row["dance_words"], "title": snap.title[:300], "text_sample": snap.text[:1500],
                "notes": snap.error[:500] or None, "screenshot": row["screenshot"] or None})
            print(f"{v:9} {s['name']}  ({snap.cookie}, {len(snap.text)} chars{', ' + snap.blocked if snap.blocked else ''})",
                  flush=True)
    write_report(rows, args)
    return 0


def summarize(rows: list[dict], title: str) -> str:
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    cookies = {}
    for r in rows:
        cookies[r["cookie_banner"]] = cookies.get(r["cookie_banner"], 0) + 1
    walls = {}
    for r in rows:
        if r["blocked_by"]:
            walls[r["blocked_by"]] = walls.get(r["blocked_by"], 0) + 1
    order = ["OK", "NO DATES", "EMPTY", "BLOCKED", "ERROR", "ROBOTS"]
    lines = [f"## {title}", f"{len(rows)} sources checked, no Claude tokens used.", "",
             "| Result | Sources | Meaning |", "|---|---|---|"]
    lines += [f"| {k} | {counts[k]} | {EXPLAIN[k]} |" for k in order if k in counts]
    lines += ["", "Cookie banners: " + ", ".join(f"{v} {k}" for k, v in sorted(cookies.items(), key=lambda x: -x[1]))]
    if walls:
        lines.append("Blocked by: " + ", ".join(f"{v} {k}" for k, v in sorted(walls.items(), key=lambda x: -x[1])))
    bad = [r for r in rows if r["verdict"] in ("BLOCKED", "EMPTY", "ERROR", "ROBOTS") or r["cookie_banner"] == "banner remains"]
    if bad:
        lines += ["", "### Needs a look", "| Source | Priority | Result | Detail | Screenshot |", "|---|---|---|---|---|"]
        for r in sorted(bad, key=lambda r: (r["priority"] or 9, r["verdict"])):
            detail = r["blocked_by"] or r["error"] or f"{r['text_chars']} characters, cookie banner: {r['cookie_banner']}"
            lines.append(f"| [{r['source']}]({r['url']}) | {r['priority']} | {r['verdict']} | "
                         f"{detail.replace('|', '/')[:150]} | {r['screenshot']} |")
    return "\n".join(lines)


def write_report(rows, args):
    md = summarize(rows, f"Preflight, shard {args.shard + 1}/{args.shards}")
    print(md)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    base = f"reports/preflight-{stamp}-shard{args.shard}"
    with open(base + ".json", "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    with open(base + ".csv", "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")


if __name__ == "__main__":
    sys.exit(main())
