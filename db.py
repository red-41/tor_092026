"""Small Supabase REST client (PostgREST) using the service key.

The collector writes only rows in import_staging_performances and the
status/next-check fields of sources. Live tables are changed only by database
functions, called from review.py: promote_staging_performances() and apply_review().
"""
import datetime as dt
import hashlib

import httpx

import config


def _client() -> httpx.Client:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return httpx.Client(
        base_url=config.SUPABASE_URL + "/rest/v1",
        headers={
            "apikey": config.SUPABASE_SERVICE_KEY,
            "Content-Type": "application/json",
            # legacy service_role keys are JWTs (eyJ...); new sb_secret_ keys go in apikey only
            **({"Authorization": "Bearer " + config.SUPABASE_SERVICE_KEY}
               if config.SUPABASE_SERVICE_KEY.startswith("eyJ") else {}),
        },
        timeout=60,
    )


ACTIVE = "todo,ok,partial,failed,waiting,blocked"


def _shard(rows: list[dict], shard: int, shards: int) -> list[dict]:
    """Spread-by-hash split (used for the preflight check of every source)."""
    if shards <= 1:
        return rows
    return [s for s in rows if int(hashlib.md5(s["id"].encode()).hexdigest(), 16) % shards == shard]


def _deal(rows: list[dict], shard: int, shards: int, also: list[dict] = ()) -> list[dict]:
    """Deal sources round-robin, like cards, so every job gets the same number (by tier).
    `also` are sources checked in the last hours: no longer due, but kept in the deal so a job that starts a little
    later than the others still deals exactly the same hands."""
    if shards <= 1:
        return rows
    deck = {r["id"]: r for r in list(also) + list(rows)}
    order = sorted(deck.values(), key=lambda r: (9 if r.get("priority") is None else r["priority"], r["id"]))
    mine = {r["id"] for i, r in enumerate(order) if i % shards == shard}
    return [r for r in rows if r["id"] in mine]


def _recently_checked(c: httpx.Client, hours: float, priority: int | None, name: str | None) -> list[dict]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
    r = c.get("/source_checks", params={"select": "source_id", "kind": "eq.collect", "checked_at": f"gte.{since}",
                                        "limit": "5000"})
    r.raise_for_status()
    ids = sorted({x["source_id"] for x in r.json() if x.get("source_id")})
    rows = []
    for i in range(0, len(ids), 150):
        params = {"select": "id,priority,name", "id": f"in.({','.join(ids[i:i + 150])})"}
        if priority is not None:
            params["priority"] = f"eq.{priority}"
        if name:
            params["name"] = f"ilike.*{name}*"
        r = c.get("/sources", params=params)
        r.raise_for_status()
        rows += r.json()
    return rows


def due_sources(limit: int, priority: int | None = None, name: str | None = None,
                shard: int = 0, shards: int = 1, include_new: bool = True, status: str | None = None) -> list[dict]:
    """Sources whose next check is due, most important and least recently collected first.
    With status, every source in that status regardless of due date (used to retry blocked sites from home)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    params = {
        "select": "*",
        "status": f"in.({ACTIVE})",
        "or": f"(next_check_at.is.null,next_check_at.lte.{now})",
        "order": "priority.asc,last_collected_at.asc.nullsfirst,name.asc",
        "limit": "5000",
    }
    if priority is not None:
        params["priority"] = f"eq.{priority}"
    if status:
        params["status"] = f"eq.{status}"
        params.pop("or")
    if name:
        params = {"select": "*", "name": f"ilike.*{name}*", "limit": "5"}
    if not include_new and not name:
        params["schedule_url"] = "not.is.null"
    with _client() as c:
        r = c.get("/sources", params=params)
        r.raise_for_status()
        rows = r.json()
        also = [] if shards <= 1 or status else _recently_checked(c, 6, priority, name)
    return _deal(rows, shard, shards, also)[:limit]


def all_sources(name: str | None = None, shard: int = 0, shards: int = 1) -> list[dict]:
    """Every source that is still in use (for the preflight check)."""
    params = {"select": "*", "status": "not.in.(closed,moved,excluded)", "order": "priority.asc,name.asc", "limit": "5000"}
    if name:
        params["name"] = f"ilike.*{name}*"
    with _client() as c:
        r = c.get("/sources", params=params)
        r.raise_for_status()
        return _shard(r.json(), shard, shards)


def upcoming_count(source_id: str) -> int | None:
    """How many future performances this site accounts for: what it lists (even when another site's row is the
    one shown), what plays on its own stages and its company's shows (view source_coverage). Counting only rows
    credited to the site undercounted venues whose shows were first found on a company or festival site."""
    try:
        with _client() as c:
            r = c.get("/source_coverage", params={"select": "total", "source_id": f"eq.{source_id}"})
            if r.status_code < 400 and r.json():
                return int(r.json()[0]["total"])
            now = dt.datetime.now(dt.timezone.utc).isoformat()       # older database without the view
            r = c.get("/performances", params={"select": "id", "source_id": f"eq.{source_id}",
                                               "performance_date": f"gte.{now}"},
                      headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"})
            if r.status_code >= 400:
                return None
            total = r.headers.get("content-range", "*/0").split("/")[-1]
            return int(total) if total.isdigit() else None
    except Exception:
        return None


def rpc(name: str, params: dict | None = None, timeout: float = 600):
    """Call a database function and return its result."""
    with _client() as c:
        r = c.post(f"/rpc/{name}", json=params or {}, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{name}: {r.status_code} {r.text[:500]}")
        return r.json() if r.content else None


def cache_get(url: str, role: str, text_hash: str, model: str, max_age_days: int = 28) -> dict | None:
    """Last reading of this exact page text, if it is recent and made with the same model."""
    try:
        with _client() as c:
            r = c.get("/collector_page_cache", params={"select": "text_hash,result,model,updated_at",
                                                        "url": f"eq.{url}", "role": f"eq.{role}"})
            if r.status_code >= 400 or not r.json():
                return None
            row = r.json()[0]
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(row["updated_at"])
        if row["text_hash"] == text_hash and row["model"] == model and age.days < max_age_days:
            return row["result"]
    except Exception:
        return None
    return None


def cache_peek(url: str, role: str) -> dict | None:
    """Whatever was last read for this page, changed or not (text_hash, result, model, updated_at)."""
    try:
        with _client() as c:
            r = c.get("/collector_page_cache", params={"select": "text_hash,result,model,updated_at",
                                                        "url": f"eq.{url}", "role": f"eq.{role}"})
            if r.status_code >= 400 or not r.json():
                return None
            return r.json()[0]
    except Exception:
        return None


def cache_put(url: str, role: str, text_hash: str, model: str, result: dict) -> None:
    try:
        with _client() as c:
            c.post("/collector_page_cache", params={"on_conflict": "url,role"},
                   json={"url": url, "role": role, "text_hash": text_hash, "model": model, "result": result,
                         "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                   headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    except Exception:
        pass


def known_venues(country: str | None) -> list[dict]:
    """Existing venue names in a country, so the model can reuse exact spellings."""
    if not country:
        return []
    with _client() as c:
        r = c.get("/theaters", params={"select": "name,city,aliases", "country": f"eq.{country}", "limit": "400"})
        r.raise_for_status()
        return r.json()


def update_source(source_id: str, fields: dict) -> None:
    with _client() as c:
        r = c.patch("/sources", params={"id": f"eq.{source_id}"}, json=fields,
                    headers={"Prefer": "return=minimal"})
        r.raise_for_status()


def record_check(row: dict) -> None:
    """One result row in source_checks (preflight or collect). Never stops the run if it fails."""
    import os
    row = {**row, "run_id": os.environ.get("GITHUB_RUN_ID") or row.get("run_id") or "local"}
    try:
        with _client() as c:
            c.post("/source_checks", json=row, headers={"Prefer": "return=minimal"})
    except Exception:
        pass


def dedupe_key(source_host: str, row: dict, run_date: str) -> str:
    parts = [source_host, row.get("venue") or "", row.get("date") or "", row.get("time") or "",
             row.get("title") or "", run_date]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def stage(rows: list[dict]) -> int:
    """Insert rows into the staging table. Duplicates (same dedupe_key) are ignored."""
    if not rows:
        return 0
    with _client() as c:
        r = c.post("/import_staging_performances", params={"on_conflict": "dedupe_key"}, json=rows,
                   headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
        r.raise_for_status()
    return len(rows)


def upsert_runs(rows: list[dict]) -> None:
    """Runs with missing dates (production_runs), keyed by run_key. Never stops the run if it fails."""
    if not rows:
        return
    try:
        with _client() as c:
            c.post("/production_runs", params={"on_conflict": "run_key"}, json=rows,
                   headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    except Exception:
        pass
