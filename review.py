"""Publish what was collected, with a duplicate check in between (runs after the collect jobs).

1. Staging rows are published by the database (promote_staging_performances). A row that is clearly a show
   already on the site becomes one more listing of it; a row that might be one is held for review.
2. Published shows are searched for pairs that may be the same performance (find_duplicate_candidates).
3. Every held row, candidate pair and conflict between trusted sites goes to Claude with the evidence: what each
   site says, and the text of the listing pages. Claude answers same / different / which listing is wrong.
4. Confident answers are applied (apply_review). The rest stay open for a person, with Claude's reasoning.

Usage:
  python review.py                 # publish, check, review, apply
  python review.py --dry           # publish nothing, ask Claude, apply nothing, write the report
  python review.py --no-publish    # only review what is already waiting
  python review.py --limit 50      # at most 50 cases of each kind
"""
import argparse
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import time

import httpx

import config
import db

PUBLISH_CHUNK = 40           # staging rows per database call (made smaller by itself if a call runs too long)
MIN_CONFIDENCE = float(os.environ.get("REVIEW_MIN_CONFIDENCE", "0.85"))
PAGE_CHARS = 5000            # text of each listing page sent along (the part around the title)
PAGES_PER_CASE = 4
REVIEW_MINUTES = float(os.environ.get("REVIEW_MINUTES", "150"))

SYSTEM = """You check a European dance listings database for duplicates. Several websites list the same
performance: the host theatre, a festival, the touring company. Each performance must appear once.

You get one case at a time:
- "held": a new listing from one website that might be a performance already published (candidates).
- "pair": two published rows that might be the same performance.
- "conflict": one performance whose trusted websites disagree on its start time or venue.

The same performance means: the same work or programme, by the same company, in the same city, on the same
date and at the same start (a matinee and an evening show are two performances). Websites describe one
performance differently, and that alone does not make it a different one:
- Venue names differ: a festival or company site may give the building ("Royal Danish Theatre"), the stage
  ("Old Stage", "Gamle Scene"), a local-language name, or the festival's own name for the place.
- Titles differ: company name added ("Afanador - Ballet Nacional de España"), translated, festival label,
  programme title vs. piece title.
- A site gives no time, or a time exactly 1 or 2 hours off (a time zone copy), while the host venue gives the
  real time. The host venue's own website is right about its own time and stage.
Different performances: different works (pieces of one mixed bill are separate rows and are not duplicates of
each other), different companies, different start times at one venue (matinee and evening), different stages
with different shows, a talk or workshop next to the show.

Which row to keep for a duplicate: the one listed by the host venue's own website (rank 1), otherwise by a
festival (rank 2), otherwise by the company (rank 3); if equal, the one with a start time and the more
complete credits.

For a conflict: name the listing(s) that are wrong (ignore_listings), for example a company site's building
name or a stale time when the host venue's own page says otherwise, or a listing attached to the wrong
performance. If the listings only word the same place differently, or both could be right, answer dismiss.

Use only the evidence given. If it does not settle the question, answer "unsure" and say what is missing.
If the pages show that neither row is a real performance (for example no show at all at that date or time), answer
"unsure" and say so plainly: a person will remove the wrong rows. Never answer "different" in that case.
Confidence is your probability that the verdict is right. Always answer by calling record_verdict."""

TOOL = {
    "name": "record_verdict",
    "description": "Record the verdict for this case.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["same", "different", "ignore_listings", "dismiss", "unsure"],
                        "description": "held and pair: same, different or unsure. conflict: ignore_listings, dismiss or unsure."},
            "keep": {"type": "string", "description": "held: id of the candidate it is. pair: id of the row to keep."},
            "ignore_listing_ids": {"type": "array", "items": {"type": "integer"},
                                   "description": "conflict with ignore_listings: listing_id values that are wrong."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "description": "One or two sentences, citing the evidence."},
        },
        "required": ["verdict", "confidence", "reason"],
    },
}


# ---------- page evidence ----------

_robots = None


def _allowed(url: str) -> bool:
    global _robots
    if _robots is None:
        from browse import Robots          # same robots.txt rules as the collector
        _robots = Robots()
    try:
        return _robots.allowed(url)
    except Exception:
        return False


def page_text(url: str) -> str:
    """Visible text of a page (plain HTTP, no browser), or '' when it cannot be read."""
    if not url or not url.startswith("http") or not _allowed(url):
        return ""
    try:
        r = httpx.get(url, timeout=25, follow_redirects=True,
                      headers={"User-Agent": config.USER_AGENT or config.BOT_UA, "Accept-Language": "en,*;q=0.5"})
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
            return ""
        t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r.text)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        return re.sub(r"\s+", " ", html.unescape(t)).strip()
    except Exception:
        return ""


def around(text: str, words: list[str], limit: int = PAGE_CHARS) -> str:
    """The parts of a long page around the title words, so the dates and venue near the title are kept."""
    if len(text) <= limit:
        return text
    low = text.lower()
    spans = []
    for w in {w.lower() for w in words if w and len(w) >= 4}:
        for m in re.finditer(re.escape(w), low):
            spans.append((max(0, m.start() - 600), min(len(text), m.end() + 900)))
    if not spans:
        return text[:limit]
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = " … ".join(text[a:b] for a, b in merged)
    return out[:limit]


def case_urls(case: dict) -> list[str]:
    rows = []
    if case["kind"] == "held":
        rows.append(case["new_listing"])
        for c in case.get("candidates") or []:
            rows += c.get("listed_by") or []
    elif case["kind"] == "pair":
        for side in ("a", "b"):
            rows += (case.get(side) or {}).get("listed_by") or []
    else:
        rows += (case.get("performance") or {}).get("listed_by") or []
    seen, urls = set(), []
    for r in sorted(rows, key=lambda r: r.get("rank") or 0):
        u = r.get("url")
        if u and u not in seen and not re.search(r"\.(pdf|jpg|png)$", u, re.I):
            seen.add(u)
            urls.append(u)
    return urls[:PAGES_PER_CASE]


def case_words(case: dict) -> list[str]:
    titles = []
    if case["kind"] == "held":
        titles += [case["new_listing"].get("title"), case["new_listing"].get("work")]
        titles += [c.get("title") for c in case.get("candidates") or []]
    elif case["kind"] == "pair":
        titles += [(case.get(s) or {}).get("title") for s in ("a", "b")]
    else:
        titles.append((case.get("performance") or {}).get("title"))
    return [w for t in titles if t for w in re.findall(r"\w+", t)]


# ---------- Claude ----------

def ask(client, model: str, case: dict, pages: dict[str, str]) -> dict:
    evidence = json.dumps(case, ensure_ascii=False, default=str, indent=1)
    page_part = "\n\n".join(f"Page {u}:\n{t}" for u, t in pages.items() if t) or "(no page text could be read)"
    user = (f"Today: {dt.date.today().isoformat()}\nCase:\n{evidence}\n\n"
            f"Text of the listing pages (the parts around the title):\n{page_part}")
    resp = client.messages.create(
        model=model, max_tokens=1500, tools=[TOOL], tool_choice={"type": "tool", "name": "record_verdict"},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}])
    for block in resp.content:
        if block.type == "tool_use":
            return dict(block.input)
    return {"verdict": "unsure", "confidence": 0, "reason": "no answer"}


def to_call(case: dict, v: dict) -> dict:
    """The apply_review arguments for a verdict, with checks: anything malformed or not confident is 'unsure'."""
    kind, verdict = case["kind"], v.get("verdict")
    conf = float(v.get("confidence") or 0)
    note = f"Claude ({conf:.2f}): {v.get('reason', '')}".strip()
    call = {"p_kind": kind, "p_id": case["id"], "p_verdict": "unsure", "p_keep": None, "p_ignore": None, "p_note": note}
    allowed = {"held": {"same", "different"}, "pair": {"same", "different"}, "conflict": {"ignore_listings", "dismiss"}}[kind]
    if verdict not in allowed or conf < MIN_CONFIDENCE:
        return call
    if verdict == "same":
        ids = ([c["id"] for c in case.get("candidates") or []] if kind == "held"
               else [(case.get("a") or {}).get("id"), (case.get("b") or {}).get("id")])
        if v.get("keep") not in ids:
            call["p_note"] = note + " [no valid row to keep given]"
            return call
        call["p_keep"] = v["keep"]
    if verdict == "ignore_listings":
        valid = {r.get("listing_id") for r in (case.get("performance") or {}).get("listed_by") or []}
        ids = [i for i in v.get("ignore_listing_ids") or [] if i in valid]
        if not ids or len(ids) >= len(valid):
            call["p_note"] = note + " [listings to ignore not valid]"
            return call
        call["p_ignore"] = ids
    call["p_verdict"] = verdict
    return call


# ---------- steps ----------

def publish(dry: bool, deadline: float) -> dict:
    counts: dict[str, int] = {}
    if dry:
        rows = db.rpc("promote_staging_performances", {"dry_run": True, "max_rows": 500}) or []
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        return counts
    chunk = PUBLISH_CHUNK
    while time.time() < deadline:
        try:
            rows = db.rpc("promote_staging_performances", {"dry_run": False, "max_rows": chunk}) or []
        except RuntimeError as e:
            # the database stops any single call after a few seconds: smaller chunks, the work is the same
            if "57014" in str(e) or "statement timeout" in str(e):
                if chunk == 1:
                    raise
                chunk = max(1, chunk // 3)
                continue
            raise
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        if len(rows) < chunk:
            break
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--no-pages", action="store_true", help="do not read the listing pages")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()

    start = time.time()
    deadline = start + REVIEW_MINUTES * 60
    run = f"auto-{dt.date.today().isoformat()}"
    report = {"run": run, "dry": a.dry, "published": {}, "cases": []}

    if not a.dry:
        report["stages_added"] = db.rpc("refresh_source_venues")
    if not a.no_publish:
        report["published"] = publish(a.dry, start + REVIEW_MINUTES * 60 * 0.4)
        print("published:", report["published"])
    if not a.dry:
        report["housekeeping"] = db.rpc("review_housekeeping")
        report["new_pairs"] = db.rpc("find_duplicate_candidates", {"p_run": run})
        print("housekeeping:", report["housekeeping"], "new pairs:", report["new_pairs"])

    cases = db.rpc("review_queue", {"p_limit": a.limit}) or []
    print(f"{len(cases)} cases to review")
    if cases and not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set: cases stay open for a person.")
        cases = []
    client = None
    if cases:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=8, timeout=300)

    tally: dict[str, int] = {}
    for case in cases:
        if time.time() > deadline:
            print("time is up: the rest waits for the next run")
            break
        pages = {} if a.no_pages else {u: around(page_text(u), case_words(case)) for u in case_urls(case)}
        try:
            v = ask(client, config.MODEL, case, pages)
        except Exception as e:                      # credit, network: stop asking, keep what is done
            print("Claude call failed:", str(e)[:300])
            break
        call = to_call(case, v)
        try:
            result = "not applied (dry run)" if a.dry else db.rpc("apply_review", call)
        except Exception as e:                      # one failed case must not stop the others
            result = f"error: {str(e)[:300]}"
            call = {**call, "p_verdict": "error"}
        key = f"{case['kind']}:{call['p_verdict']}"
        tally[key] = tally.get(key, 0) + 1
        report["cases"].append({"kind": case["kind"], "id": case["id"], "verdict": v, "applied": call["p_verdict"],
                                "result": result})
        print(f"{case['kind']:8} {case['id']}: {v.get('verdict')} ({v.get('confidence')}) -> {result}")

    report["tally"] = tally
    out = pathlib.Path("reports")
    out.mkdir(exist_ok=True)
    (out / f"review-{run}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Publish and review {run}\n\nPublished: {report['published']}\n\n"
                    + "".join(f"- {k}: {n}\n" for k, n in sorted(tally.items()))
                    + "\n'unsure' cases are waiting in match_review, duplicate_review and performance_conflicts.\n")
    print("tally:", tally)
    return 0


if __name__ == "__main__":
    sys.exit(main())
