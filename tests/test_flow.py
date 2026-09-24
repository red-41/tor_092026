"""End-to-end flow with a fake browser and a fake Claude: listing -> detail pages -> staging rows."""
import datetime as dt
from types import SimpleNamespace

from collector import db, run
from collector.browse import Snapshot
from collector.extract import Extractor

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()


class FakeBrowser:
    def load(self, url):
        return Snapshot(url=url, final_url=url, title="t", text="page " + url, og_image="https://x.org/og.jpg")


class FakeMessages:
    def create(self, **kw):
        text = kw["messages"][0]["content"]
        if "URL: https://x.org/ballet/giselle" in text:
            data = None
        elif "URL: https://x.org/\n" in text:
            data = {"page_status": "not_a_schedule", "productions": [], "detail_links": [],
                    "schedule_url_guess": "https://x.org/ballet"}
        elif "URL: https://x.org/ballet" in text:
            data = {"page_status": "listing_without_dates", "productions": [],
                    "detail_links": ["https://x.org/ballet/giselle"], "next_page_url": None}
        if data is None:
            data = {"page_status": "has_performances", "detail_links": [], "productions": [
                {"title": "Giselle", "work": "Giselle", "work_is_story": True, "choreographer": "Akram Khan",
                 "tags": ["Contemporary"], "performances": [
                     {"venue": "Opera", "date": FUTURE, "time": "19:30"},
                     {"venue": "Opera", "date": FUTURE, "time": None}]}]}
        block = SimpleNamespace(type="tool_use", input=data)
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_flow(monkeypatch):
    monkeypatch.setattr(db, "known_venues", lambda country: [{"name": "Opera", "city": "X"}])
    ex = Extractor.__new__(Extractor)
    ex.client = SimpleNamespace(messages=FakeMessages())
    ex.model, ex.input_tokens, ex.output_tokens = "fake", 0, 0
    src = {"id": "s1", "name": "X Opera", "website": "https://x.org/", "schedule_url": None, "priority": 1,
           "city": "X", "country": "Y", "timezone": "Europe/Paris", "kind": "Opera & ballet house"}
    res = run.collect_source(FakeBrowser(), ex, src)
    assert res["schedule_url"] == "https://x.org/ballet"
    assert res["status"] == "ok" and res["read_method"] == "detail_pages"
    assert len(res["items"]) == 1 and res["items"][0]["time"] == "19:30"    # untimed duplicate dropped
    assert res["items"][0]["image_url"] == "https://x.org/og.jpg"
    rows = run.to_staging(res, "2026-09-24")
    assert rows[0]["work_is_story"] is True and rows[0]["tickets_url"] == "https://x.org/ballet/giselle"
