"""Cost controls (16 Oct 2026): the spending cap, Haiku with a second look by Sonnet, production pages that would
add nothing, season announcements, and the model comparison."""
import datetime as dt
from types import SimpleNamespace

import pytest

import budget
import config
import db
import lenses
import run
from browse import Snapshot
from extract import Batch, Extractor, OutOfCredit, _credit_error
from test_rolling import PAGES, Claude, Sites, show, src, world  # noqa: F401  (world is a fixture)

FUTURE = (dt.date.today() + dt.timedelta(days=40)).isoformat()
LATER = (dt.date.today() + dt.timedelta(days=47)).isoformat()


# ---------- prices and the ledger ----------

def test_prices_per_model_batch_and_long_prompts():
    assert budget.cost_usd("claude-haiku-5-5", 50_000, 0) == pytest.approx(0.005)
    assert budget.cost_usd("claude-haiku-5-5", 0, 10_000, batch=True) == pytest.approx(0.0025)
    assert budget.cost_usd("claude-sonnet-5", 50_000, 10_000) == pytest.approx(0.2)
    assert budget.cost_usd("claude-haiku-5-5", 120_000, 0) == pytest.approx(0.06)      # 5x above 100k tokens
    assert budget.cost_usd("some-new-model", 50_000, 0) == pytest.approx(0.2)          # unknown: dearest known
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=3000,
                            cache_creation_input_tokens=0)
    assert budget.usage_cost("claude-sonnet-5-5", usage, batch=True) == pytest.approx(
        (1000 * 2 + 200 * 10 + 3000 * 0.10) / 1e6 / 2)


def _ledger(monkeypatch):
    rows = []

    def rpc(name, params=None, timeout=600):
        if name == "spend_reserve":
            month = sum(r["p_usd"] for r in rows)
            if month + params["p_usd"] > params["p_month_cap"]:
                return {"ok": False, "why": "month", "month": month, "run": month,
                        "month_cap": params["p_month_cap"], "run_cap": params["p_run_cap"]}
            rows.append(params)
            return {"ok": True, "month": month + params["p_usd"], "run": month + params["p_usd"]}
        rows.append(params)
    monkeypatch.setattr(db, "rpc", rpc)
    return rows


def test_budget_pays_from_blocks_and_stops_at_the_monthly_cap(monkeypatch):
    rows = _ledger(monkeypatch)
    b = budget.Budget(run_id="r", month_cap=0.6, run_cap=10, block=0.25)
    for _ in range(10):
        b.spend(0.05)                                         # 0.50 from two blocks of 0.25
    assert len(rows) == 2
    with pytest.raises(budget.BudgetReached):
        b.spend(0.05)                                         # a third block would make 0.75 > 0.60
    assert "monthly spending cap of $0.6" in b.stopped
    with pytest.raises(budget.BudgetReached):
        b.spend(0.0001)                                       # stays stopped
    b.answered(0.05, 0.01)                                    # an answer cost less than estimated
    b.close()
    assert rows[-1]["p_kind"] == "adjust" and rows[-1]["p_usd"] == pytest.approx(-0.04)


def test_budget_gives_back_what_it_did_not_use(monkeypatch):
    rows = _ledger(monkeypatch)
    b = budget.Budget(run_id="r", month_cap=100, run_cap=10, block=0.25)
    b.spend(0.05)
    b.close()
    assert [r.get("p_kind") for r in rows] == [None, "release"] and rows[-1]["p_usd"] == pytest.approx(-0.20)


def test_no_ledger_means_no_spending(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database down")
    monkeypatch.setattr(db, "rpc", broken)
    with pytest.raises(budget.BudgetReached):
        budget.Budget(run_id="r").spend(0.01)


def test_console_spend_limit_counts_as_out_of_credit():
    assert _credit_error(Exception("You have reached your specified API usage limits. "
                                   "You will regain access on 2026-11-01 at 00:00 UTC."))
    assert _credit_error(Exception("Your credit balance is too low to access the Anthropic API."))
    assert not _credit_error(Exception("Overloaded"))


class Refuser(budget.NoBudget):
    """A cap that allows n requests."""
    def __init__(self, n):
        self.n, self.used_estimate = n, 0.0

    def spend(self, usd):
        if self.n <= 0:
            raise budget.BudgetReached("the per-run spending cap of $1 is reached")
        self.n -= 1
        self.used_estimate += usd


def test_the_cap_stops_a_run_like_running_out_of_credit(world, monkeypatch, tmp_path):
    claude, writes, mp = world
    sources = [src("D1", "https://d1.org/"), src("D2", "https://d2.org/"), src("D3", "https://d3.org/")]
    mp.setattr(db, "due_sources", lambda *a, **k: [dict(s) for s in sources])
    mp.setattr(run, "Browser", Sites())
    mp.setattr(config, "SUPABASE_URL", "https://db.example")
    mp.setattr(budget, "Budget", lambda **k: Refuser(4))        # 3 schedule pages + 1 production page
    mp.setattr(db, "pending_all", lambda: [])
    assert run.main([]) == 0
    assert len(writes["staged"]) == 1                          # what was paid for is kept
    report = next(tmp_path.joinpath("reports").glob("run-*.md")).read_text()
    assert "per-run spending cap of $1 is reached" in report
    assert claude.batched == 4                                 # nothing was sent beyond the cap


def test_a_job_starting_at_the_cap_sends_nothing(world, tmp_path):
    claude, writes, mp = world
    mp.setattr(db, "due_sources", lambda *a, **k: [src("D1", "https://d1.org/")])
    mp.setattr(run, "Browser", Sites())
    mp.setattr(config, "SUPABASE_URL", "https://db.example")
    _ledger(mp)
    mp.setattr(config, "MONTHLY_BUDGET_USD", 0.0)
    assert run.main([]) == 0
    assert claude.batched == 0 and claude.live == 0 and not writes["staged"] and not writes["updated"]
    report = next(tmp_path.joinpath("reports").glob("run-*.md")).read_text()
    assert "monthly spending cap of $0.0 is reached" in report and "| D1 | not read |" in report


# ---------- Haiku first, Sonnet for a second look ----------

class TwoModels(Claude):
    """The small model leaves shows out on unsure.org (and says so); the big model reads it right."""
    def __init__(self):
        super().__init__()
        self.models = []

    def _answer(self, params):
        resp = super()._answer(params)
        self.models.append(params["model"])
        text = params["messages"][0]["content"]
        if params["model"] == "small" and "URL: https://unsure.org/" in text:
            resp.content[0].input = {"page_status": "has_performances", "productions": [], "detail_links": [],
                                     "complete": False, "unsure": "the rest of the list is on the page"}
        return resp


PAGES["https://unsure.org/"] = show("Raymonda")


@pytest.fixture
def two(world):
    claude, writes, mp = world
    tm = TwoModels()
    ex = Extractor.__new__(Extractor)
    ex.client, ex.model, ex.escalate_model = SimpleNamespace(messages=tm), "small", "big"
    ex.input_tokens = ex.output_tokens = ex.calls = ex.cache_hits = 0
    ex.out_of_credit = False
    ex.escalations = ex.answered = 0
    mp.setattr(run, "Extractor", lambda model=None: ex)
    cached = {}
    mp.setattr(db, "cache_put", lambda url, role, h, model, data: cached.__setitem__(url, model))
    return tm, ex, writes, mp, cached


def test_doubtful_reading_gets_a_second_look_live(two):
    tm, ex, writes, mp, cached = two
    res = run.collect_source(Sites(), ex, src("U", "https://unsure.org/"))
    assert [i["title"] for i in res["items"]] == ["Raymonda"]
    assert tm.models == ["small", "big"] and ex.escalations == 1
    assert cached["https://unsure.org/"] == "big"              # the better reading is the one kept


def test_doubtful_reading_gets_a_second_look_in_the_batch(two, tmp_path):
    tm, ex, writes, mp, cached = two
    mp.setattr(db, "due_sources", lambda *a, **k: [src("U", "https://unsure.org/"), src("A", "https://a.org/")])
    mp.setattr(run, "Browser", Sites())
    mp.setattr(db, "pending_all", lambda: [])
    assert run.main([]) == 0
    assert sorted(r["title"] for r in writes["staged"]) == ["Bolero", "Raymonda"]
    assert tm.models[:2] == ["small", "big"]                   # the key check tries both models
    assert tm.models[2:].count("big") == 1                     # only the doubtful page went to the big model
    assert "1 pages given a second look" in next(tmp_path.joinpath("reports").glob("run-*.md")).read_text()


def test_second_look_reasons_and_its_limit(monkeypatch):
    ex = SimpleNamespace(model="small", escalate_model="big", escalations=0, answered=0)
    monkeypatch.setattr(db, "cache_peek", lambda key, role: None)
    assert run.second_look(ex, {"_truncated": True, "productions": []}, "u", "r") == "answer cut off"
    assert run.second_look(ex, {"complete": False, "productions": []}, "u", "r") == "the reading says it left shows out"
    assert run.second_look(ex, {"unsure": "I recorded only the first production", "productions": []},
                           "u", "r").startswith("unsure")
    assert run.second_look(ex, {"unsure": "times are not published", "productions": []}, "u", "r") is None
    d = dt.date.today() + dt.timedelta(days=30)
    days = [{"date": (d + dt.timedelta(days=i)).isoformat()} for i in range(6)]
    assert run.second_look(ex, {"productions": [{"title": "Drøm", "performances": days}]}, "u", "r").startswith(
        "dates of a range written out")
    timed = [{**x, "time": "19:30"} for x in days]
    assert run.second_look(ex, {"productions": [{"title": "Drøm", "performances": timed}]}, "u", "r") is None
    assert run.second_look(ex, {"productions": [], "_model": "big"}, "u", "r") is None     # already the big model
    prev = {"result": {"productions": [{"title": "X", "performances": [{"date": FUTURE}] * 6}]}}
    monkeypatch.setattr(db, "cache_peek", lambda key, role: prev)
    one = {"productions": [{"title": "X", "performances": [{"date": FUTURE}]}]}
    assert run.second_look(ex, one, "u", "r") == "found 1 shows, 6 at the last reading"
    ex.escalations = 50                                       # over the share allowed for second looks
    assert run.second_look(ex, {"_truncated": True}, "u", "r") is None


def test_unchanged_page_reuses_a_stronger_models_reading(monkeypatch):
    seen = []
    monkeypatch.setattr(db, "cache_get", lambda url, role, h, models: seen.append(set(models)) or {"productions": []})
    ex = SimpleNamespace(model="claude-haiku-5-5", cache_hits=0)
    snap = Snapshot(url="https://x.org/", final_url="https://x.org/", title="t", text="x" * 500, status=200)
    run.read_page(ex, snap, src("X", "https://x.org/"), [], run.LISTING, True)
    assert {"claude-haiku-5-5", "claude-sonnet-5", "claude-sonnet-5-5"} <= seen[0]


def test_long_pages_are_trimmed_below_the_haiku_price_step():
    ex = Extractor.__new__(Extractor)
    ex.model = "claude-haiku-5-5"
    snap = Snapshot(url="https://x.org/", final_url="https://x.org/", title="t", status=200,
                    text="Спектакль балет " * 20000)
    snap.links = [(f"link {i}", f"https://x.org/p/{i}") for i in range(400)]
    snap.sitemap = [f"https://x.org/event/{i}" for i in range(200)]
    params = ex.request_params(snap, src("X", "https://x.org/"), [], run.LISTING)
    est = budget.estimate_tokens(params["messages"][0]["content"]) + budget.estimate_tokens(str(params["tools"]))
    assert est <= config.MAX_PROMPT_TOKENS


# ---------- production pages that would add nothing ----------

def _bill(title, url, date, choreographer):
    return {"title": title, "tags": ["Classical"], "detail_url": url, "choreographer": choreographer,
            "performances": [{"venue": "Main Stage", "date": date, "time": "19:30"}]}


PAGES["https://r.org/"] = {
    "page_status": "has_performances",
    "productions": [_bill("Giselle", "https://r.org/giselle", FUTURE, "Akram Khan"),
                    _bill("Swan Lake", "https://r.org/swan", FUTURE, None),
                    _bill("Swan Lake", "https://r.org/swan-2", LATER, None),
                    {**_bill("Onegin", "https://r.org/onegin", FUTURE, "John Cranko"),
                     "performances": [{"venue": "Main Stage", "date": FUTURE, "time": None}]}],
    "detail_links": ["https://r.org/giselle", "https://r.org/swan", "https://r.org/swan-2", "https://r.org/en/swan",
                     "https://r.org/onegin"]}
PAGES["https://r.org/swan"] = {"page_status": "has_performances", "detail_links": [], "productions": [
    {**_bill("Swan Lake", "https://r.org/swan", FUTURE, "Marius Petipa")}]}
PAGES["https://r.org/onegin"] = {"page_status": "has_performances", "detail_links": [], "productions": [
    _bill("Onegin", "https://r.org/onegin", FUTURE, "John Cranko")]}


def test_pages_that_would_add_nothing_are_not_opened(world):
    claude, writes, mp = world
    ex = Extractor.__new__(Extractor)
    ex.client, ex.model = SimpleNamespace(messages=claude), "fake"
    ex.input_tokens = ex.output_tokens = ex.calls = ex.cache_hits = 0
    ex.out_of_credit = False
    sites = Sites()
    res = run.collect_source(sites, ex, src("R", "https://r.org/"))
    # Giselle: dates, times and choreographer on the schedule -> skipped. Swan Lake: credits missing -> one page
    # for the production, its second page skipped, and /en/swan is the same page. Onegin: a time missing -> opened.
    assert sites.opened == ["https://r.org/", "https://r.org/swan", "https://r.org/onegin"]
    assert res["skipped_pages"] == 3
    swans = [i for i in res["items"] if i["title"] == "Swan Lake"]
    assert len(swans) == 2 and all(i["choreographer"] == "Marius Petipa" for i in swans)   # credits shared
    assert next(i for i in res["items"] if i["title"] == "Giselle")["choreographer"] == "Akram Khan"


def test_touring_company_pages_are_always_read(world):
    claude, writes, mp = world
    ex = Extractor.__new__(Extractor)
    ex.client, ex.model = SimpleNamespace(messages=claude), "fake"
    ex.input_tokens = ex.output_tokens = ex.calls = ex.cache_hits = 0
    ex.out_of_credit = False
    sites = Sites()
    company = {**src("R", "https://r.org/"), "kind": "Dance company"}
    run.collect_source(sites, ex, company)
    assert "https://r.org/giselle" in sites.opened and "https://r.org/swan-2" in sites.opened
    assert "https://r.org/en/swan" not in sites.opened        # the same page twice is still read once


def test_same_page_under_another_address():
    assert lenses.page_key("https://www.x.org/fr/spectacle/a/") == lenses.page_key("http://x.org/en/spectacle/a")
    assert lenses.page_key("https://x.org/e?id=1&utm_source=n") == lenses.page_key("https://x.org/e?id=1")
    assert lenses.page_key("https://x.org/e?id=1") != lenses.page_key("https://x.org/e?id=2")
    assert lenses.page_key("https://x.org/enfants/a") != lenses.page_key("https://x.org/a")


# ---------- season announcements ----------

def test_announcement_dates_are_read_and_checked():
    st = {"today": dt.date(2026, 10, 16)}
    run.note_announcement(st, {"announcement": {"what": "2027/28 season", "date": "2027-03-03", "quote": "q"}}, "u")
    run.note_announcement(st, {"announcement": {"what": "festival", "month": "2027-01", "quote": "q"}}, "u")
    run.note_announcement(st, {"announcement": {"what": "old", "date": "2026-09-01", "quote": "q"}}, "u")   # past
    run.note_announcement(st, {"announcement": {"what": "far", "date": "2028-06-01", "quote": "q"}}, "u")   # too far
    assert [a["date"] for a in st["announcements"]] == ["2027-03-03", "2027-01-01"]


def test_next_check_follows_the_announcement():
    today, soon = dt.date(2026, 10, 16), dt.date(2026, 11, 1)
    tier3, tier0 = {"priority": 3}, {"priority": 0}
    assert run.announcement_days(tier3, 30, 5, soon, today) == 17      # shows on: look again the day after
    assert run.announcement_days(tier0, 7, 5, soon, today) == 7        # weekly anyway
    assert run.announcement_days(tier3, 30, 0, dt.date(2026, 12, 1), today) == 47   # nothing on: wait for it
    assert run.announcement_days(tier3, 30, 0, dt.date(2027, 6, 1), today) == 60    # but at most 60 days
    assert run.announcement_days(tier0, 7, 0, dt.date(2027, 6, 1), today) == 30     # tiers 0-1: at most 30
    assert run.announcement_days(tier3, 0, 0, soon, today) == 0        # due again at once stays so


PAGES["https://ann.org/"] = {"page_status": "not_published_yet", "productions": [], "detail_links": [],
                             "announcement": {"what": "2027 festival programme",
                                              "date": (dt.date.today() + dt.timedelta(days=20)).isoformat(),
                                              "quote": "Le programme 2027 sera dévoilé le ..."}}


def test_announcement_is_saved_on_the_source(world, tmp_path):
    claude, writes, mp = world
    mp.setattr(db, "due_sources", lambda *a, **k: [{**src("F", "https://ann.org/"), "priority": 3, "kind": "Festival"}])
    mp.setattr(run, "Browser", Sites())
    mp.setattr(db, "pending_all", lambda: [])
    assert run.main([]) == 0
    fields = writes["updated"]["F"]
    when = dt.date.today() + dt.timedelta(days=20)
    assert fields["announcement_at"] == when.isoformat() and "2027 festival programme" in fields["announcement_note"]
    due = dt.datetime.fromisoformat(fields["next_check_at"]).date()
    assert due == when + dt.timedelta(days=1)
    assert "### Announced" in next(tmp_path.joinpath("reports").glob("run-*.md")).read_text()


# ---------- sharing credits, comparison ----------

def test_dates_without_credits_take_them_from_the_same_production():
    items = [{"title": "Swan Lake", "company": None, "venue": "Main", "choreographer": None, "description": None,
              "image_url": None},
             {"title": "Swan Lake", "company": None, "venue": "Main", "choreographer": "Petipa", "description": "d",
              "image_url": "i"},
             {"title": "Swan Lake", "company": "Other Ballet", "venue": "Main", "choreographer": None,
              "description": None, "image_url": None}]
    run.fill_from_siblings(items)
    assert items[0]["choreographer"] == "Petipa" and items[0]["description"] == "d"
    assert items[2]["choreographer"] is None                  # another company's production is left alone


def test_comparison_records_both_readings(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "compare_put", rows.append)
    snap = Snapshot(url="https://x.org/", final_url="https://x.org/", title="t", text="x" * 500, status=200)
    h = run.page_hash(snap)
    peek = {"text_hash": h, "model": "claude-sonnet-5", "result": {"productions": [], "page_status": "x"}}
    monkeypatch.setattr(db, "cache_peek", lambda key, role: peek)
    answers = []

    class Ex:
        model, escalate_model, compare, compare_fresh_left = "small", "big", True, 1
        answered = escalations = 0
        pay = budget.NoBudget()

        def read(self, snap, source, venues, role, model=None):
            answers.append(model or "small")
            return {"productions": [], "_model": model or "small", "_usage": {"input_tokens": 1}}
    ex = Ex()
    run.read_page(ex, snap, src("X", "https://x.org/"), [], run.LISTING, True)
    assert rows[0]["ref_from_cache"] is True and rows[0]["ref_model"] == "claude-sonnet-5" and answers == ["small"]
    assert "second_look" in rows[0] and rows[0]["final_result"] == rows[0]["result"]    # no second look needed
    peek["text_hash"] = "changed"
    run.read_page(ex, snap, src("X", "https://x.org/"), [], run.LISTING, True)
    assert rows[1]["ref_from_cache"] is False and rows[1]["ref_model"] == "big" and answers == ["small", "small", "big"]
    run.read_page(ex, snap, src("X", "https://x.org/"), [], run.LISTING, True)
    assert rows[2]["ref_result"] is None                      # fresh reference readings used up


def test_compare_mode_reads_live_and_writes_nothing_to_the_site(world, tmp_path):
    claude, writes, mp = world
    rows = []
    mp.setattr(db, "compare_sources", lambda n: [src("A", "https://a.org/"), src("D1", "https://d1.org/")])
    mp.setattr(db, "compare_put", rows.append)
    mp.setattr(db, "cache_peek", lambda url, role: None)
    mp.setattr(run, "Browser", Sites())
    assert run.main(["--compare", "2"]) == 0
    assert claude.batched == 0 and claude.live >= 4            # straight answers, no batches
    assert not writes["staged"] and not writes["updated"]      # nothing reaches the site
    assert {r["url"] for r in rows} >= {"https://a.org/", "https://a.org/dance", "https://d1.org/", "https://d1.org/show"}


def test_answer_form_is_forced_only_where_the_model_accepts_it():
    from extract import retarget, tool_choice
    assert tool_choice("claude-haiku-5-5") == {"type": "auto"}           # thinks first (a forced call skips it)
    assert tool_choice("claude-sonnet-5") == {"type": "tool", "name": "record_page"}
    assert tool_choice("claude-sonnet-5-5") == {"type": "auto"}          # refuses forced tool calls
    ex = Extractor.__new__(Extractor)
    ex.model = "claude-haiku-5-5"
    snap = Snapshot(url="https://x.org/", final_url="https://x.org/", title="t", text="x" * 500, status=200)
    params = ex.request_params(snap, src("X", "https://x.org/"), [], run.LISTING)
    assert params["tool_choice"] == {"type": "auto"}
    assert retarget(params, "claude-opus-5-5")["tool_choice"] == {"type": "auto"}
    assert retarget(params, "claude-sonnet-5")["tool_choice"] == {"type": "tool", "name": "record_page"}
    # thinking is billed as output: the estimate allows for it
    haiku_est = budget.estimate_request(retarget(params, "claude-haiku-5-5"))
    forced = dict(params, tool_choice={"type": "tool", "name": "record_page"})
    assert haiku_est > budget.estimate_request(forced)


def test_a_reply_in_prose_gets_a_second_look(monkeypatch):
    from extract import Extractor as E
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking", thinking="..."),
                                                            SimpleNamespace(type="text", text="Here are the shows")])
    data = E.parse(resp)
    assert data["_no_answer"] and data["_truncated"]
    monkeypatch.setattr(db, "cache_peek", lambda key, role: None)
    ex = SimpleNamespace(model="small", escalate_model="big", escalations=0, answered=0)
    assert run.second_look(ex, data, "u", "r") == "no answer in the form"
    ok = E.parse(SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="thinking", thinking="..."),
        SimpleNamespace(type="tool_use", input={"page_status": "has_performances", "productions": [], "detail_links": []})]))
    assert ok["page_status"] == "has_performances" and not ok.get("_no_answer")


def test_english_titles_of_the_last_reading_are_kept():
    prev = {"productions": [{"title": "Carnations", "original_title": "Nelken"},
                            {"title": "This World", "original_title": "Este Mundo"},
                            {"title": "Giselle", "original_title": None}]}
    new = {"productions": [{"title": "Nelken", "original_title": None},             # native title kept by the model
                           {"title": "The World", "original_title": "Este Mundo"},   # another translation
                           {"title": "Giselle", "original_title": None},
                           {"title": "Drøm", "original_title": None}]}               # not in the last reading
    assert run.keep_english_titles(new, prev) == 2
    assert [p["title"] for p in new["productions"]] == ["Carnations", "This World", "Giselle", "Drøm"]
    assert new["productions"][0]["original_title"] == "Nelken"
    assert run.keep_english_titles(new, None) == 0


def test_comparison_does_the_second_look_and_records_what_a_run_keeps(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "compare_put", rows.append)
    snap = Snapshot(url="https://x.org/", final_url="https://x.org/", title="t", text="x" * 500, status=200)
    six = {"productions": [{"title": "Swan Lake", "performances": [{"date": FUTURE, "time": "19:30"}] * 6}]}
    monkeypatch.setattr(db, "cache_peek", lambda key, role: {"text_hash": "old", "model": "claude-sonnet-5", "result": six})
    answers = []

    class Ex:
        model, escalate_model, compare, compare_fresh_left = "small", "big", True, 5
        answered = escalations = 0
        pay = budget.NoBudget()

        def read(self, snap, source, venues, role, model=None):
            answers.append(model or "small")
            if model == "big":
                return {**six, "_model": "big"}
            return {"productions": [{"title": "Swan Lake", "performances": [{"date": FUTURE, "time": "19:30"}]}],
                    "_model": "small"}
    final = run.read_page(Ex(), snap, src("X", "https://x.org/"), [], run.LISTING, True)
    assert answers == ["small", "big"]                         # far fewer shows than last time: second look
    row = rows[0]
    assert row["second_look"].startswith("found 1 shows") and row["final_result"]["_model"] == "big"
    assert row["ref_model"] == "big" and row["ref_from_cache"] is False   # the second look is the reference too
    assert final["_model"] == "big"                            # the run follows what a normal run keeps
