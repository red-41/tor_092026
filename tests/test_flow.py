"""End-to-end flow with a fake browser and a fake Claude: listing -> detail pages -> staging rows."""
import datetime as dt
from types import SimpleNamespace

import pytest

from collector import db, run
from collector.browse import Snapshot
from collector.extract import Extractor

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()
LONG = "Season programme with dates. " * 80


class FakeBrowser:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)

    def load(self, url):
        if url in self.blocked:
            return Snapshot(url=url, final_url=url, title="Just a moment...", text="Checking your browser",
                            status=403, blocked="cloudflare", screenshot="reports/screens/x.jpg")
        return Snapshot(url=url, final_url=url, title="t", text=LONG + url, og_image="https://x.org/og.jpg", status=200)


class FakeMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
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
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.fixture
def env(monkeypatch):
    cache = {}
    state = {"upcoming": 0}
    monkeypatch.setattr(db, "known_venues", lambda country: [{"name": "Opera", "city": "X"}])
    monkeypatch.setattr(db, "upcoming_count", lambda sid: state["upcoming"])
    monkeypatch.setattr(db, "cache_get", lambda url, role, h, model: cache.get((url, role, h)))
    monkeypatch.setattr(db, "cache_put", lambda url, role, h, model, data: cache.__setitem__((url, role, h), data))
    ex = Extractor.__new__(Extractor)
    ex.client = SimpleNamespace(messages=FakeMessages())
    ex.model, ex.input_tokens, ex.output_tokens, ex.calls, ex.cache_hits = "fake", 0, 0, 0, 0
    return ex, state


SRC = {"id": "s1", "name": "X Opera", "website": "https://x.org/", "schedule_url": None, "priority": 1,
       "city": "X", "country": "Y", "timezone": "Europe/Paris", "kind": "Opera & ballet house"}


def test_flow(env):
    ex, _ = env
    res = run.collect_source(FakeBrowser(), ex, SRC)
    assert res["schedule_url"] == "https://x.org/ballet"
    assert res["status"] == "ok" and res["read_method"] == "detail_pages" and not res["check"]
    assert len(res["items"]) == 1 and res["items"][0]["time"] == "19:30"    # untimed duplicate dropped
    assert res["items"][0]["image_url"] == "https://x.org/og.jpg"
    rows = run.to_staging(res, "2026-09-24")
    assert rows[0]["work_is_story"] is True and rows[0]["tickets_url"] == "https://x.org/ballet/giselle"


def test_unchanged_pages_are_not_sent_to_claude_again(env):
    ex, _ = env
    run.collect_source(FakeBrowser(), ex, SRC)
    first = ex.client.messages.calls
    res = run.collect_source(FakeBrowser(), ex, SRC)
    assert ex.client.messages.calls == first and ex.cache_hits == first
    assert len(res["items"]) == 1


def test_blocked_site_costs_no_tokens_and_is_marked_blocked(env):
    ex, _ = env
    res = run.collect_source(FakeBrowser(blocked={"https://x.org/"}), ex, SRC)
    assert res["status"] == "blocked" and res["blocked"] == ["cloudflare"]
    assert ex.client.messages.calls == 0 and res["screens"]


def test_blocked_detail_page_makes_result_partial(env):
    ex, _ = env
    src = {**SRC, "schedule_url": "https://x.org/ballet"}
    res = run.collect_source(FakeBrowser(blocked={"https://x.org/ballet/giselle"}), ex, src)
    assert res["status"] == "failed" and res["blocked"] == ["cloudflare"]


def test_far_fewer_than_before_is_flagged(env):
    ex, state = env
    state["upcoming"] = 30
    res = run.collect_source(FakeBrowser(), ex, SRC)
    assert res["check"] and res["notes"][0].startswith("CHECK: found 1, but 30 upcoming")
