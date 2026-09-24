"""Small Supabase REST client (PostgREST) using the service key.

Only three things are written: rows in import_staging_performances, and the
status/next-check fields of sources. Live tables are changed only by the
database function promote_staging_performances().
"""
import datetime as dt
import hashlib

import httpx

from . import config


def _client() -> httpx.Client:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return httpx.Client(
        base_url=config.SUPABASE_URL + "/rest/v1",
        headers={
            "apikey": config.SUPABASE_SERVICE_KEY,
            "Authorization": "Bearer " + config.SUPABASE_SERVICE_KEY,
            "Content-Type": "application/json",
        },
        timeout=60,
    )


def due_sources(limit: int, priority: int | None = None, name: str | None = None,
                shard: int = 0, shards: int = 1, include_new: bool = True) -> list[dict]:
    """Sources whose next check is due, most important and least recently collected first."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    params = {
        "select": "*",
        "status": "in.(todo,ok,partial,failed,waiting)",
        "or": f"(next_check_at.is.null,next_check_at.lte.{now})",
        "order": "priority.asc,last_collected_at.asc.nullsfirst,name.asc",
        "limit": str(limit * max(shards, 1)),
    }
    if priority:
        params["priority"] = f"eq.{priority}"
    if name:
        params = {"select": "*", "name": f"ilike.*{name}*", "limit": "5"}
    if not include_new and not name:
        params["schedule_url"] = "not.is.null"
    with _client() as c:
        r = c.get("/sources", params=params)
        r.raise_for_status()
        rows = r.json()
    if shards > 1:
        rows = [s for s in rows if int(hashlib.md5(s["id"].encode()).hexdigest(), 16) % shards == shard]
    return rows[:limit]


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
