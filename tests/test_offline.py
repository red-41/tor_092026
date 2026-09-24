"""Offline checks: validation, merging, staging rows and the browser on a local page."""
import datetime as dt
import pathlib

from collector import db
from collector.extract import clean_performance, merge
from collector.run import to_staging

TODAY = dt.date(2026, 9, 24)


def item(**kw):
    base = {"title": "Giselle", "date": "2026-10-01", "time": "19:30", "tags": ["Classical"], "venue": "Opera"}
    base.update(kw)
    return base


def test_rejects_past_and_bad_dates():
    assert clean_performance(item(date="2026-09-01"), TODAY) is None
    assert clean_performance(item(date="01/10/2026"), TODAY) is None
    assert clean_performance(item(title=""), TODAY) is None


def test_time_and_tags_are_normalised():
    c = clean_performance(item(time="7.30pm", tags=["Premiere"]), TODAY)
    assert c["time"] is None                       # unparseable time becomes Time TBC, never guessed
    assert c["tags"][0] == "Contemporary"          # a genre tag is always present
    assert "Premiere" in c["tags"]


def test_merge_prefers_timed_rows_and_keeps_pieces():
    a = clean_performance(item(time=None), TODAY)
    b = clean_performance(item(time="19:30"), TODAY)
    c = clean_performance(item(time="14:00"), TODAY)           # matinee same day
    p1 = clean_performance(item(title="Cacti", program="Common Ground", program_position=1), TODAY)
    p2 = clean_performance(item(title="IMPASSE", program="Common Ground", program_position=2), TODAY)
    out = merge([a, b, c, b, p1, p2])
    giselle = [o for o in out if o["title"] == "Giselle"]
    assert sorted(o["time"] for o in giselle) == ["14:00", "19:30"]
    assert {o["title"] for o in out} == {"Giselle", "Cacti", "IMPASSE"}


def test_to_staging_rows():
    src = {"id": "00000000-0000-0000-0000-000000000001", "website": "https://www.example.org/", "city": "Paris",
           "country": "France", "timezone": "Europe/Paris"}
    it = clean_performance(item(time=None), TODAY)
    rows = to_staging({"source": src, "items": [it]}, "2026-09-24")
    r = rows[0]
    assert r["source"] == "www.example.org" and r["time_tbc"] is True and r["status"] == "pending"
    assert r["city"] == "Paris" and r["timezone"] == "Europe/Paris"
    assert r["dedupe_key"] == db.dedupe_key("www.example.org", it, "2026-09-24")
    assert set(rows[0]) == set(to_staging({"source": src, "items": [clean_performance(item(), TODAY)]}, "x")[0])


def test_browser_on_local_page(tmp_path):
    from collector import config
    config.DELAY_SECONDS = 0
    from collector.browse import Browser
    html = tmp_path / "page.html"
    html.write_text("""<html><head><title>Season</title>
      <meta property="og:image" content="/img/poster.jpg">
      <script type="application/ld+json">{"@context":"https://schema.org","@graph":[
        {"@type":"TheaterEvent","name":"Swan Lake","startDate":"2026-10-02T19:30"}]}</script></head>
      <body><div id="cookie"><button onclick="document.getElementById('cookie').remove()">Reject all</button></div>
      <h1>Ballet</h1><a href="/swan-lake">Swan Lake</a><p>2 October 19:30</p></body></html>""")
    with Browser() as br:
        snap = br.load(html.as_uri())
    assert not snap.error, snap.error
    assert "Swan Lake" in snap.text and "Reject all" not in snap.text   # cookie banner was refused
    assert snap.events and snap.events[0]["name"] == "Swan Lake"
    assert snap.og_image.endswith("/img/poster.jpg")
