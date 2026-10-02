"""Whole runs with fake sites: the collector follows dance pages and overview pages, and saves every site
as soon as its own pages are answered (so a job that dies part way keeps what it finished)."""
import datetime as dt
import re
from types import SimpleNamespace

import pytest

import config
import db
import run
from browse import Snapshot
from extract import Batch, Extractor

FUTURE = (dt.date.today() + dt.timedelta(days=40)).isoformat()
FILL = "Season programme and news. " * 60


def show(title):
    return {"page_status": "has_performances", "detail_links": [], "productions": [
        {"title": title, "tags": ["Contemporary"], "choreographer": "X",
         "performances": [{"venue": "Main Stage", "date": FUTURE, "time": "19:30"}]}]}


PAGES = {
    # A: the homepage shows no dance, but Claude points at the dance category page
    "https://a.org/": {"page_status": "no_dance_programme", "productions": [], "detail_links": [],
                       "schedule_url_guess": "https://a.org/dance"},
    "https://a.org/dance": show("Bolero"),
    # B: homepage -> ballet overview (taken for a production page) -> the shows
    "https://b.org/": {"page_status": "listing_without_dates", "productions": [], "detail_links": ["https://b.org/ballet"]},
    "https://b.org/ballet": {"page_status": "listing_without_dates", "productions": [],
                             "detail_links": ["https://b.org/ballet/giselle", "https://b.org/ballet/swan"]},
    "https://b.org/ballet/giselle": show("Giselle"),
    "https://b.org/ballet/swan": show("Swan Lake"),
    # C: homepage with one show and a link to the full dance calendar
    "https://c.org/": {**show("Onegin"), "listing_links": ["https://c.org/calendar"]},
    "https://c.org/calendar": show("Carmen"),
}
for i in range(1, 4):   # D1..D3: one production page each
    PAGES[f"https://d{i}.org/"] = {"page_status": "listing_without_dates", "productions": [],
                                   "detail_links": [f"https://d{i}.org/show"]}
    PAGES[f"https://d{i}.org/show"] = show(f"Piece {i}")


class Sites:
    def __init__(self, die_at=None):
        self.die_at, self.opened = die_at, []

    def load(self, url):
        if url == self.die_at:
            raise SystemExit("the job was stopped")          # like GitHub stopping the job
        self.opened.append(url)
        return Snapshot(url=url, final_url=url, title="t", text=FILL + url, status=200)

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Claude:
    """Answers from PAGES; live calls and batches both count."""
    def __init__(self):
        self.live, self.batched, self.created, self.store = 0, 0, 0, {}
        self.batches = SimpleNamespace(create=self._create, retrieve=lambda bid: SimpleNamespace(processing_status="ended"),
                                       results=lambda bid: iter(self.store[bid]), cancel=lambda bid: None)

    def _answer(self, params):
        url = re.search(r"URL: (\S+)\n", params["messages"][0]["content"])
        data = PAGES.get(url.group(1) if url else "", {"page_status": "not_a_schedule", "productions": [], "detail_links": []})
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    def create(self, **params):
        self.live += 1
        return self._answer(params)

    def _create(self, requests):
        self.created += 1
        self.batched += len(requests)
        bid = f"b{self.created}"
        self.store[bid] = [SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
            type="succeeded", message=self._answer(r["params"]))) for r in requests]
        return SimpleNamespace(id=bid)


def src(name, url):
    return {"id": name, "name": name, "website": url, "schedule_url": None, "priority": 0, "city": "X",
            "country": "Y", "timezone": "Europe/Paris", "kind": "Theatre"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cache, writes = {}, {"staged": [], "updated": {}}
    claude = Claude()
    ex = Extractor.__new__(Extractor)
    ex.client, ex.model = SimpleNamespace(messages=claude), "fake"
    ex.input_tokens = ex.output_tokens = ex.calls = ex.cache_hits = 0
    ex.out_of_credit = False
    monkeypatch.setattr(run, "Extractor", lambda model=None: ex)
    monkeypatch.setattr(db, "known_venues", lambda country: [])
    monkeypatch.setattr(db, "upcoming_count", lambda sid: 0)
    monkeypatch.setattr(db, "cache_get", lambda url, role, h, model: cache.get((url, role, h)))
    monkeypatch.setattr(db, "cache_put", lambda url, role, h, model, data: cache.__setitem__((url, role, h), data))
    monkeypatch.setattr(db, "cache_peek", lambda url, role: None)
    monkeypatch.setattr(db, "stage", lambda rows: writes["staged"].extend(rows) or len(rows))
    monkeypatch.setattr(db, "update_source", lambda sid, fields: writes["updated"].__setitem__(sid, fields))
    monkeypatch.setattr(db, "record_check", lambda row: None)
    writes["runs"] = []
    monkeypatch.setattr(db, "upsert_runs", lambda rows: writes["runs"].extend(rows))
    monkeypatch.setattr(config, "USE_BATCH", True)
    monkeypatch.setattr(Batch, "POLL_S", 0)
    return claude, writes, monkeypatch


def titles(writes, sid):
    return sorted(r["title"] for r in writes["staged"] if r["source_id"] == sid)


def test_dance_page_overview_pages_and_calendar_links_are_all_followed(world):
    claude, writes, mp = world
    mp.setattr(run, "Browser", Sites())
    mp.setattr(db, "due_sources", lambda *a, **k: [src("A", "https://a.org/"), src("B", "https://b.org/"),
                                                   src("C", "https://c.org/")])
    assert run.main([]) == 0
    assert titles(writes, "A") == ["Bolero"]
    assert writes["updated"]["A"]["status"] == "ok"
    assert writes["updated"]["A"]["schedule_url"] == "https://a.org/dance"      # starts there next time
    assert titles(writes, "B") == ["Giselle", "Swan Lake"]                       # two levels below the homepage
    assert titles(writes, "C") == ["Carmen", "Onegin"]
    assert claude.batched >= 6                                                   # production pages at half price


def test_each_site_is_saved_as_soon_as_it_is_done(world):
    claude, writes, mp = world
    mp.setattr(config, "BATCH_SEND_AT", 1)
    mp.setattr(run, "Browser", Sites(die_at="https://d3.org/show"))
    mp.setattr(db, "due_sources", lambda *a, **k: [src(f"D{i}", f"https://d{i}.org/") for i in range(1, 4)])
    with pytest.raises(SystemExit):
        run.main([])
    assert sorted(writes["updated"]) == ["D1", "D2"]          # finished before the job died: kept
    assert titles(writes, "D1") == ["Piece 1"] and titles(writes, "D2") == ["Piece 2"]


def test_jobs_get_even_shares_and_a_late_job_deals_the_same_hands():
    rows = [{"id": f"{i:03d}", "priority": 0} for i in range(45)] + [{"id": f"z{i}", "priority": 1} for i in range(24)]
    hands = [db._deal(rows, j, 12) for j in range(12)]
    assert sorted(len(h) for h in hands) == [5] * 3 + [6] * 9
    assert sorted(sum(1 for r in h if r["priority"] == 0) for h in hands) == [3] * 3 + [4] * 9
    done = rows[:10]                                            # saved by faster jobs in the meantime
    late = [db._deal(rows[10:], j, 12, also=done) for j in range(12)]
    assert [[r["id"] for r in h if r not in done] for h in hands] == [[r["id"] for r in h] for h in late]



# ------------------------------------------------------------------------------------------ Phase 1: date hunt etc.
def D(n):
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


def perf(venue, *days, city=None, tz=None):
    return [{"venue": venue, "city": city, "date": D(d), "time": "19:30", "timezone": tz} for d in days]


PAGES.update({
    # E: a production page with two runs whose dates are missing; the hunt reads the dates page and the host venue's page
    "https://e.org/": {"page_status": "listing_without_dates", "productions": [], "detail_links": ["https://e.org/rituals"]},
    "https://e.org/rituals": {"page_status": "has_performances", "detail_links": [], "productions": [
        {"title": "Rituals", "tags": ["Contemporary"], "performances": perf("Palais Garnier", 30),
         "runs": [{"venue": "Palais Garnier", "city": "Paris", "start": D(30), "end": D(40), "expected_performances": 4,
                   "dates_page_url": "https://tickets.e.org/rituals"}]},
        {"title": "Tour Piece", "tags": ["Contemporary"], "performances": [],
         "runs": [{"venue": "Sadler's Wells", "city": "London", "start": D(50), "end": D(52),
                   "venue_event_url": "https://venue.org/tour-piece", "dates_page_url": "https://www.ticketmaster.co.uk/tour"}]}]},
    "https://tickets.e.org/rituals": {"page_status": "has_performances", "detail_links": [], "productions": [
        {"title": "Rituals", "tags": ["Contemporary"], "performances": perf("Palais Garnier", 30, 32, 35, 40)}]},
    "https://venue.org/tour-piece": {"page_status": "has_performances", "detail_links": [], "productions": [
        {"title": "Tour Piece", "tags": ["Contemporary"], "performances": perf("Sadler's Wells", 50, 52, city="London")}]},
    # I: the page announces 12 performances but lists 7
    "https://i.org/": {"page_status": "listing_without_dates", "productions": [], "detail_links": ["https://i.org/corsaire"]},
    "https://i.org/corsaire": {"page_status": "has_performances", "detail_links": [], "expected_performances": 12,
                               "productions": [{"title": "Le Corsaire", "tags": ["Classical"],
                                                "performances": perf("Opera House", 20, 21, 22, 23, 24, 25, 26)}]},
    # J: a calendar showing one month only, no next link
    "https://j.org/": {"page_status": "has_performances", "detail_links": [], "productions": [
        {"title": "Swan Lake", "tags": ["Classical"], "performances": perf("Main Stage", 5, 12, 19)}]},
    # H: a touring company page (no city given) and a performance abroad with its time zone
    "https://h.org/": {"page_status": "has_performances", "detail_links": [], "productions": [
        {"title": "Tour", "tags": ["Contemporary"],
         "performances": perf("Teatro X", 15) + perf("War Memorial Opera House", 60, city="San Francisco",
                                                     tz="America/Los_Angeles")}]},
})


def test_date_hunt_finds_missing_dates_on_the_dates_page_and_the_host_venues_page(world):
    claude, writes, mp = world
    sites = Sites()
    mp.setattr(run, "Browser", sites)
    mp.setattr(db, "due_sources", lambda *a, **k: [src("E", "https://e.org/")])
    assert run.main([]) == 0
    staged = [r for r in writes["staged"] if r["source_id"] == "E"]
    rituals = sorted(r["performance_date"] for r in staged if r["title"] == "Rituals")
    assert rituals == [D(30), D(32), D(35), D(40)]
    tour = [r for r in staged if r["title"] == "Tour Piece"]
    assert len(tour) == 2 and {r["date_source"] for r in tour} == {"venue_site"}
    assert not any("ticketmaster" in u for u in sites.opened)              # never a ticket seller
    runs = {r["title"]: r for r in writes["runs"]}
    assert runs["Rituals"]["status"] == "complete" and runs["Rituals"]["found_count"] == 4
    assert "booking" in runs["Rituals"]["lenses_tried"]
    assert runs["Tour Piece"]["status"] == "complete" and "venue_site" in runs["Tour Piece"]["lenses_tried"]


def test_lower_tiers_record_the_run_but_do_not_hunt(world):
    claude, writes, mp = world
    sites = Sites()
    mp.setattr(run, "Browser", sites)
    mp.setattr(db, "due_sources", lambda *a, **k: [{**src("E", "https://e.org/"), "priority": 2}])
    run.main([])
    assert "https://tickets.e.org/rituals" not in sites.opened and "https://venue.org/tour-piece" not in sites.opened
    runs = {r["title"]: r for r in writes["runs"]}
    assert runs["Rituals"]["status"] == "range_only" and runs["Tour Piece"]["status"] == "range_only"


def test_fewer_dates_than_announced_is_a_partial_run(world):
    claude, writes, mp = world
    mp.setattr(run, "Browser", Sites())
    mp.setattr(db, "due_sources", lambda *a, **k: [src("I", "https://i.org/")])
    run.main([])
    (r,) = writes["runs"]
    assert r["title"] == "Le Corsaire" and r["status"] == "partial" and r["found_count"] == 7 and r["expected_count"] == 12


def test_calendar_by_month_is_read_to_the_end_of_the_season_for_core_tiers(world):
    claude, writes, mp = world
    today = dt.date.today()
    nxt_y, nxt_m = (today.year + (today.month == 12), today.month % 12 + 1)
    PAGES["https://g.org/"] = {**show("Nutcracker"), "next_page_url": f"https://g.org/cal?month={nxt_y}-{nxt_m:02d}"}
    months = []
    y, m = nxt_y, nxt_m
    for _ in range(12):
        months.append(f"https://g.org/cal?month={y}-{m:02d}")
        y, m = (y + (m == 12), m % 12 + 1)
    # a house that has published the whole season: every month is read (tiers 0-1); tier 2 just follows "next"
    for u in months:
        PAGES[u] = show("Giselle " + u[-7:])
    for tier, expected in ((0, 13), (2, 2)):
        sites = Sites()
        mp.setattr(run, "Browser", sites)
        mp.setattr(db, "due_sources", lambda *a, **k: [{**src("G", "https://g.org/"), "priority": tier}])
        run.main([])
        assert len([u for u in sites.opened if "g.org" in u]) == expected, tier
    # a house that has published two months: reading stops three months after the last month with shows
    h = [u.replace("g.org", "mm.org") for u in months]
    PAGES["https://mm.org/"] = {**show("Nutcracker"), "next_page_url": h[0]}
    for u in h[:2]:
        PAGES[u] = show("Giselle " + u[-7:])
    sites = Sites()
    mp.setattr(run, "Browser", sites)
    mp.setattr(db, "due_sources", lambda *a, **k: [{**src("MM", "https://mm.org/"), "priority": 0}])
    run.main([])
    assert [u for u in sites.opened if "cal?" in u] == h[:5]
    for u in months + h[:2] + ["https://mm.org/"]:
        PAGES.pop(u, None)


def test_a_dance_category_that_pays_off_is_remembered_and_read_next_time(world):
    claude, writes, mp = world
    sites = Sites()
    mp.setattr(run, "Browser", sites)
    mp.setattr(db, "due_sources", lambda *a, **k: [src("C", "https://c.org/")])
    run.main([])
    assert writes["updated"]["C"]["extra_schedule_urls"] == ["https://c.org/calendar"]
    sites2 = Sites()
    mp.setattr(run, "Browser", sites2)
    PAGES["https://c.org/"] = show("Onegin")                           # the homepage no longer links to it
    mp.setattr(db, "due_sources", lambda *a, **k: [{**src("C", "https://c.org/"), "extra_schedule_urls": ["https://c.org/calendar"]}])
    run.main([])
    assert "https://c.org/calendar" in sites2.opened


def test_one_month_calendar_is_flagged(world):
    claude, writes, mp = world
    mp.setattr(run, "Browser", Sites())
    mp.setattr(db, "due_sources", lambda *a, **k: [{**src("J", "https://j.org/"), "priority": 2}])
    run.main([])
    report = next(__import__("pathlib").Path("reports").glob("run-*.md")).read_text()
    assert "calendar may show one month at a time" in report


def test_touring_company_dates_keep_their_own_place_and_time_zone(world):
    claude, writes, mp = world
    mp.setattr(run, "Browser", Sites())
    company = {**src("H", "https://h.org/"), "kind": "Ballet / dance company", "city": "Leeds", "country": "United Kingdom",
               "timezone": "Europe/London"}
    mp.setattr(db, "due_sources", lambda *a, **k: [company])
    run.main([])
    rows = {r["venue"]: r for r in writes["staged"]}
    assert rows["Teatro X"]["city"] is None                           # never the company's home city
    sf = rows["War Memorial Opera House"]
    assert sf["timezone"] == "America/Los_Angeles" and sf["city"] == "San Francisco"
