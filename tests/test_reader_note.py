"""An editors' note about a site (sources.reader_note) reaches the page reader."""
from types import SimpleNamespace

from extract import Extractor

LIM = {"venues": 10, "same": 10, "other": 10, "events": 1000, "feeds": 3, "sitemap": 10, "text": 1000}


def _snap():
    return SimpleNamespace(final_url="https://www.lequai-angers.eu/eclats_1", title="ÉCLATS", og_image=None,
                           links=[("ajouter à mon agenda", "https://mobile.lequai-angers.eu/public/uploads/calendar/20261202-1900-ECLATS.ics")],
                           events=[], feeds=[], sitemap=[], text="mercredi 02 déc. 26 18:00")


def _text(source):
    ex = Extractor.__new__(Extractor)
    return ex._user_text(_snap(), source, [], "production page", LIM)


def test_note_is_sent_when_set():
    note = "Printed start times are in UTC: use the calendar file name or add 1 h in winter, 2 h in summer."
    out = _text({"name": "Le Quai", "timezone": "Europe/Paris", "reader_note": note})
    assert "Note about this site from the editors (follow it): " + note in out
    assert out.index("Time zone Europe/Paris") < out.index("Note about this site") < out.index("This page is a")


def test_no_note_line_without_a_note():
    out = _text({"name": "Le Quai", "timezone": "Europe/Paris", "reader_note": None})
    assert "Note about this site" not in out
    assert "Time zone Europe/Paris.\nThis page is a production page." in out
