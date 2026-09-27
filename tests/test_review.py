"""The in-run copy removal and the reviewer's safety checks (no network, no database)."""
import datetime as dt

import review
from extract import clean_performance, merge, time_seen

TODAY = dt.date(2026, 9, 24)


def item(**kw):
    base = {"title": "Afanador", "date": "2026-09-28", "time": "19:00", "tags": ["Contemporary"],
            "venue": "Gamle Scene", "city": "Copenhagen"}
    base.update(kw)
    return clean_performance(base, TODAY)


def test_copy_without_venue_is_dropped():
    out = merge([item(), item(venue=None), item(venue=None, time=None)])
    assert len(out) == 1 and out[0]["venue"] == "Gamle Scene"


def test_venue_less_item_kept_when_it_is_the_only_one():
    out = merge([item(venue=None), item(date="2026-09-29", venue="Gamle Scene")])
    assert len(out) == 2


def test_utc_twin_dropped_by_page_text():
    local, utc = item(time="19:00"), item(time="17:00")
    local["time_seen"] = time_seen("19:00", "Mon 28 Sep 19.00 Gamle Scene")
    utc["time_seen"] = time_seen("17:00", "Mon 28 Sep 19.00 Gamle Scene")
    out = merge([utc, local])
    assert [o["time"] for o in out] == ["19:00"]


def test_matinee_and_evening_both_kept():
    a, b = item(time="14:00"), item(time="19:30")
    a["time_seen"] = b["time_seen"] = True
    assert len(merge([a, b])) == 2


def test_twins_kept_when_page_shows_both():
    a, b = item(time="18:00"), item(time="19:00")
    a["time_seen"] = b["time_seen"] = True             # two real sessions an hour apart
    assert len(merge([a, b])) == 2


def test_time_seen_formats():
    assert time_seen("19:30", "kl. 19.30")
    assert time_seen("20:00", "20h")
    assert time_seen("19:30", "7:30 pm")
    assert not time_seen("17:30", "19:30 and 21:30")
    assert not time_seen(None, "19:30")


PAIR = {"kind": "pair", "id": 7, "a": {"id": "A", "listed_by": [{"listing_id": 1, "url": "https://x.org/a", "rank": 1}]},
        "b": {"id": "B", "listed_by": [{"listing_id": 2, "url": "https://y.org/b", "rank": 3}]}}
CONFLICT = {"kind": "conflict", "id": 3, "performance": {"id": "P", "title": "Giselle", "listed_by": [
    {"listing_id": 11, "url": "https://x.org"}, {"listing_id": 12, "url": "https://y.org"}]}}


def test_confident_same_is_applied():
    c = review.to_call(PAIR, {"verdict": "same", "keep": "A", "confidence": 0.95, "reason": "same show"})
    assert c["p_verdict"] == "same" and c["p_keep"] == "A"


def test_low_confidence_is_left_for_a_person():
    c = review.to_call(PAIR, {"verdict": "same", "keep": "A", "confidence": 0.6, "reason": "maybe"})
    assert c["p_verdict"] == "unsure" and c["p_keep"] is None


def test_keep_must_be_one_of_the_pair():
    c = review.to_call(PAIR, {"verdict": "same", "keep": "Z", "confidence": 0.99, "reason": "x"})
    assert c["p_verdict"] == "unsure"


def test_verdict_must_fit_the_case():
    assert review.to_call(PAIR, {"verdict": "dismiss", "confidence": 0.99, "reason": "x"})["p_verdict"] == "unsure"
    assert review.to_call(CONFLICT, {"verdict": "same", "confidence": 0.99, "reason": "x"})["p_verdict"] == "unsure"


def test_conflict_cannot_ignore_every_listing_or_unknown_ones():
    ok = review.to_call(CONFLICT, {"verdict": "ignore_listings", "ignore_listing_ids": [12], "confidence": 0.9, "reason": "x"})
    assert ok["p_verdict"] == "ignore_listings" and ok["p_ignore"] == [12]
    all_ = review.to_call(CONFLICT, {"verdict": "ignore_listings", "ignore_listing_ids": [11, 12], "confidence": 0.9, "reason": "x"})
    assert all_["p_verdict"] == "unsure"
    bad = review.to_call(CONFLICT, {"verdict": "ignore_listings", "ignore_listing_ids": [99], "confidence": 0.9, "reason": "x"})
    assert bad["p_verdict"] == "unsure"


def test_page_excerpt_keeps_text_around_the_title():
    text = "x" * 20000 + " Afanador 28 September 19.00 Gamle Scene " + "y" * 20000
    part = review.around(text, ["Afanador"], limit=3000)
    assert "Afanador 28 September 19.00" in part and len(part) <= 3000


def test_case_urls_best_rank_first_and_capped():
    urls = review.case_urls(PAIR)
    assert urls == ["https://x.org/a", "https://y.org/b"]


def test_main_flow_applies_only_confident_verdicts(monkeypatch, tmp_path):
    import sys
    import config
    import db
    calls = []
    queue = [PAIR, CONFLICT]

    def fake_rpc(name, params=None, timeout=600):
        calls.append((name, params))
        if name == "promote_staging_performances":
            return [{"staging_id": 1, "action": "insert", "performance_id": "X", "detail": ""}]
        if name == "review_queue":
            return queue
        if name == "apply_review":
            return "ok"
        return 0

    answers = {"pair": {"verdict": "same", "keep": "A", "confidence": 0.97, "reason": "same show"},
               "conflict": {"verdict": "ignore_listings", "ignore_listing_ids": [12], "confidence": 0.5, "reason": "?"}}
    monkeypatch.setattr(db, "rpc", fake_rpc)
    monkeypatch.setattr(review, "ask", lambda client, model, case, pages: answers[case["kind"]])
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["review.py", "--no-pages"])
    assert review.main() == 0
    names = [c[0] for c in calls]
    assert names[:4] == ["refresh_source_venues", "promote_staging_performances", "review_housekeeping",
                         "find_duplicate_candidates"]
    applied = [c[1] for c in calls if c[0] == "apply_review"]
    assert applied[0]["p_verdict"] == "same" and applied[0]["p_keep"] == "A"
    assert applied[1]["p_verdict"] == "unsure"            # 0.5 confidence: left for a person
    assert (tmp_path / "reports").exists()


def test_timezone_follows_the_country_of_the_date():
    from run import _timezone
    lisbon = {"country": "Portugal", "timezone": "Europe/Lisbon"}
    assert _timezone({"country": "Spain", "timezone": "Europe/Lisbon", "date": "2027-04-15"}, lisbon) == "Europe/Madrid"
    assert _timezone({"country": "Spain", "timezone": "Atlantic/Canary", "date": "2027-04-15"}, lisbon) == "Atlantic/Canary"
    assert _timezone({"country": "Germany", "timezone": "Europe/London", "date": "2027-04-15"}, lisbon) == "Europe/Berlin"
    assert _timezone({"country": "United States", "timezone": "America/Chicago", "date": "2027-02-04"}, lisbon) == "America/Chicago"
    assert _timezone({"country": "Belgium", "timezone": "Europe/Paris", "date": "2027-01-26"}, lisbon) == "Europe/Paris"
    assert _timezone({"country": "", "timezone": None}, lisbon) == "Europe/Lisbon"
