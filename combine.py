"""Merge the per-shard reports of one GitHub run into one summary and one spreadsheet (CSV).

  python combine.py all-reports/
"""
import csv
import glob
import json
import os
import sys

from preflight import summarize


def main(argv=None):
    folder = (argv or sys.argv[1:] or ["reports"])[0]
    pre = [r for p in sorted(glob.glob(f"{folder}/**/preflight-*.json", recursive=True)) for r in json.load(open(p))]
    run = [r for p in sorted(glob.glob(f"{folder}/**/run-*.json", recursive=True)) for r in json.load(open(p))]
    parts = []
    if pre:
        parts.append(summarize(pre, "Preflight: all sources"))
        with open(f"{folder}/preflight-all.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(pre[0]))
            w.writeheader()
            w.writerows(sorted(pre, key=lambda r: (r["verdict"] != "OK", r["priority"] or 9, r["source"])))
    if run:
        by = {}
        for r in run:
            by[r["status"]] = by.get(r["status"], 0) + 1
        found = sum(r["performances"] for r in run)
        lines = ["## Collection: all sources",
                 f"{len(run)} sources: " + ", ".join(f"{v} {k}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
                 + f". {found} performances found.", ""]
        bad = [r for r in run if r["status"] in ("blocked", "failed") or r.get("check")]
        if bad:
            lines += ["| Source | Status | Found | Already on site | Why |", "|---|---|---|---|---|"]
            for r in bad:
                lines.append(f"| {r['source']} | {r['status']} | {r['performances']} | {r.get('upcoming_before') or 0} | "
                             + "; ".join(r["notes"]).replace("|", "/")[:200] + " |")
        parts.append("\n".join(lines))
        with open(f"{folder}/collection-all.csv", "w", newline="") as f:
            keys = ["source", "status", "performances", "staged", "pages", "check", "upcoming_before",
                    "blocked", "cookies", "minutes", "notes"]
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in run:
                w.writerow({**r, "blocked": ", ".join(r.get("blocked", [])), "cookies": ", ".join(r.get("cookies", [])),
                            "notes": "; ".join(r.get("notes", []))})
    md = "\n\n".join(parts) or "No reports found."
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
