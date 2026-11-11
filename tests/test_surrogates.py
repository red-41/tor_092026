"""A page with a broken character (half of a styled letter or emoji) must not stop the job (job 16, 11 Nov 2026:
UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud835': surrogates not allowed)."""
import json
from types import SimpleNamespace

import pytest

import extract
from extract import Extractor, clean_text, page_hash

LONE = "Ultima Vez \ud835 tickets"                 # a lone half, as the browser handed it over
PAIR = "𝐔𝐥𝐭"      # "𝐔𝐥𝐭" split into halves


def test_clean_text_joins_pairs_and_replaces_lone_halves():
    assert clean_text(PAIR) == "𝐔𝐥𝐭"
    assert clean_text(LONE) == "Ultima Vez � tickets"
    assert clean_text("plain text, déjà vu") == "plain text, déjà vu"
    clean_text(LONE).encode("utf-8")               # sendable


def _snap(text):
    return SimpleNamespace(final_url="https://www.ultimavez.com/en/agenda", title="Agenda " + PAIR, og_image=None,
                           links=[(LONE, "https://www.ultimavez.com/en/agenda/x")], events=[{"name": LONE}],
                           feeds=[{"url": "https://www.ultimavez.com/api", "text": LONE}], sitemap=[], text=text)


def test_request_for_a_broken_page_can_be_sent():
    ex = Extractor.__new__(Extractor)
    ex.model = "claude-haiku-5-5"
    params = ex.request_params(_snap("14.11.2026 20:00 " + LONE), {"name": "Ultima Vez", "timezone": "Europe/Brussels"},
                               [], "schedule page")
    json.dumps(params, ensure_ascii=False).encode("utf-8")   # what the API client does: no UnicodeEncodeError
    assert "𝐔𝐥𝐭" in params["messages"][0]["content"]


def test_page_hash_of_a_broken_page():
    assert len(page_hash(_snap("14.11.2026 20:00 " + LONE))) == 64
    # an ordinary page keeps its fingerprint (saved readings stay valid)
    assert page_hash(_snap("14.11.2026 20:00")) == page_hash(_snap("14.11.2026 20:00"))


def test_unsendable_request_is_not_queued():
    """If a request still cannot be encoded, it fails for its own site only and nothing is left half queued."""
    ex = SimpleNamespace(spend=lambda params, batch: 0.0, model="m", escalate_model="m2",
                         pay=SimpleNamespace(answered=lambda *a: None))
    b = extract.Batch.__new__(extract.Batch)
    b.ex, b.requests, b._unsent, b.context, b.meta, b._packed = ex, [], [], {}, {}, {}
    with pytest.raises(UnicodeEncodeError):
        b.add({"model": "m", "messages": [{"role": "user", "content": LONE}]}, None)
    assert b._unsent == [] and b.requests == [] and b.meta == {}
    b.add({"model": "m", "messages": [{"role": "user", "content": "fine"}]}, None)
    assert len(b._unsent) == 1
