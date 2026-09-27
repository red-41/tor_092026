"""End-to-end flow with a fake browser and a fake Claude: listing -> detail pages -> staging rows."""
import datetime as dt
from types import SimpleNamespace

import pytest

import db, run
from browse import Snapshot
from extract import Extractor

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


class FakeBatches:
    def __init__(self, messages):
        self.messages, self.store, self.created = messages, {}, 0

    def create(self, requests):
        self.created += 1
        bid = f"b{self.created}"
        self.store[bid] = [SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
            type="succeeded", message=self.messages.answer(r["params"]))) for r in requests]
        return SimpleNamespace(id=bid)

    def retrieve(self, bid):
        return SimpleNamespace(processing_status="ended")

    def results(self, bid):
        return iter(self.store[bid])

    def cancel(self, bid):
        pass


class FakeMessages:
    def __init__(self):
        self.calls = 0
        self.batches = FakeBatches(self)

    def answer(self, params):
        calls = self.calls
        resp = self.create(**params)
        self.calls = calls                      # batch answers are not live calls
        return resp

    def create(self, **kw):
        self.calls += 1
        text = kw["messages"][0]["content"]
        data = None
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

    def peek(url, role):
        for (u, r, h), data in cache.items():
            if u == url and r == role:
                return {"text_hash": h, "result": data, "model": "fake",
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        return None
    monkeypatch.setattr(db, "cache_peek", peek)
    ex = Extractor.__new__(Extractor)
    ex.client = SimpleNamespace(messages=FakeMessages())
    ex.model, ex.input_tokens, ex.output_tokens, ex.calls, ex.cache_hits = "fake", 0, 0, 0, 0
    ex.out_of_credit = False
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
    assert ex.client.messages.calls == first                 # nothing new sent to Claude
    assert ex.cache_hits + res["reused"] == first            # listing pages from cache, production page reused
    assert res["reused"] == 1 and len(res["items"]) == 1


def test_batch_gives_the_same_result_at_half_price(env):
    ex, _ = env
    from extract import Batch
    batch = Batch(ex)
    res = run.collect_source(FakeBrowser(), ex, SRC, batch=batch)
    assert res["pending"] == 1 and len(batch.requests) == 1     # production page waits for the batch
    live_calls = ex.client.messages.calls
    answers = batch.run(deadline=9e18, poll_s=0)
    run.apply_batch(batch, answers, ex)
    run.finalize(res)
    assert ex.client.messages.calls == live_calls and batch.calls == 1
    assert res["status"] == "ok" and len(res["items"]) == 1 and res["items"][0]["time"] == "19:30"


def test_production_page_reread_rule():
    today = dt.date(2026, 9, 24)
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
    def prev(dates, when=fresh):
        return {"updated_at": when, "result": {"productions": [{"title": "X", "performances": [{"date": d} for d in dates]}]}}
    assert run.reusable(prev(["2026-12-01"]), today)                 # far away and read recently: skip
    assert not run.reusable(prev(["2026-10-05"]), today)             # next show within 4 weeks: read again
    assert not run.reusable(prev(["2026-12-01"], old), today)        # read over a month ago: read again
    assert not run.reusable(prev([]), today)                         # no dates known yet: read again
    assert run.reusable(prev(["2026-01-01"]), today)                 # run is over: skip
    assert not run.reusable(None, today)


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


class _BrowserCtx(FakeBrowser):
    def __init__(self, *a, **k):
        super().__init__()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def full_run(env, monkeypatch, tmp_path):
    """main() end to end with fakes: nothing leaves the machine."""
    ex, state = env
    monkeypatch.chdir(tmp_path)
    writes = {"staged": [], "updated": [], "checks": []}
    monkeypatch.setattr(run, "Browser", _BrowserCtx)
    monkeypatch.setattr(run, "Extractor", lambda model=None: ex)
    monkeypatch.setattr(db, "due_sources", lambda *a, **k: [dict(SRC)])
    monkeypatch.setattr(db, "stage", lambda rows: writes["staged"].extend(rows) or len(rows))
    monkeypatch.setattr(db, "update_source", lambda sid, fields: writes["updated"].append(fields))
    monkeypatch.setattr(db, "record_check", lambda row: writes["checks"].append(row))
    monkeypatch.setattr(run.config, "USE_BATCH", True)
    return ex, writes, tmp_path


def test_two_rounds_listing_and_production_pages_both_batched(full_run):
    ex, writes, tmp = full_run
    batches = ex.client.messages.batches
    assert run.main([]) == 0
    assert batches.created == 2                                   # round 1: schedule page, round 2: production page
    assert len(writes["staged"]) == 1 and writes["staged"][0]["performance_time"] == "19:30"
    assert writes["updated"][0]["status"] == "ok"
    report = next(tmp.joinpath("reports").glob("run-*.md")).read_text()
    assert "2 pages via the Batch API" in report and "credit ran out" not in report


def test_credit_running_out_stops_cleanly(full_run, monkeypatch):
    import anthropic, httpx
    ex, writes, tmp = full_run

    def broke(requests):
        raise anthropic.APIStatusError("Your credit balance is too low to access the Anthropic API.",
                                       response=httpx.Response(400, request=httpx.Request("POST", "https://api")), body=None)
    monkeypatch.setattr(ex.client.messages.batches, "create", broke)
    assert run.main([]) == 0
    assert writes["updated"] == [] and writes["staged"] == []     # source untouched: still due next run
    report = next(tmp.joinpath("reports").glob("run-*.md")).read_text()
    assert "credit ran out" in report and "| X Opera | not read |" in report


def test_page_fingerprint_ignores_ticket_noise_but_sees_new_shows():
    from extract import page_hash
    def snap(text):
        return Snapshot(url="u", final_url="u", title="t", text=text)
    base = page_hash(snap("Giselle\n12 October 19:30\nSwan Lake\n20 November 19:30"))
    assert page_hash(snap("Swan Lake\n20 November 19:30 Sold out\nGiselle\n12 October 19:30 - Few tickets left\nToday")) == base
    assert page_hash(snap("Giselle\n12 October 19:30\nSwan Lake\n20 November 19:30\nCarmen\n3 December 20:00")) != base
    assert page_hash(snap("Giselle\n12 October 19:30\nSwan Lake\n21 November 19:30")) != base


def test_credit_running_out_in_round_two_keeps_what_was_found(full_run, monkeypatch):
    import anthropic, httpx
    ex, writes, tmp = full_run
    fb = ex.client.messages.batches
    real = fb.create

    def second_fails(requests):
        if fb.created >= 1:
            raise anthropic.APIStatusError("Your credit balance is too low", body=None,
                                           response=httpx.Response(400, request=httpx.Request("POST", "https://api")))
        return real(requests)
    monkeypatch.setattr(fb, "create", second_fails)
    # the schedule page itself lists one dated show, so something is found before the credit runs out
    orig = ex.client.messages.create

    def create(**kw):
        resp = orig(**kw)
        if "URL: https://x.org/ballet\n" in kw["messages"][0]["content"]:
            resp.content[0].input = {"page_status": "has_performances", "detail_links": ["https://x.org/ballet/giselle"],
                                     "productions": [{"title": "Bolero", "tags": ["Contemporary"],
                                                      "performances": [{"venue": "Opera", "date": FUTURE, "time": "20:00"}]}]}
        return resp
    monkeypatch.setattr(ex.client.messages, "create", create)
    assert run.main([]) == 0
    assert [r["title"] for r in writes["staged"]] == ["Bolero"]           # kept
    assert writes["updated"][0]["status"] == "partial"
    due = dt.datetime.fromisoformat(writes["updated"][0]["next_check_at"])
    assert due < dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)  # due again straight away
    assert "credit ran out" in next(tmp.joinpath("reports").glob("run-*.md")).read_text()
