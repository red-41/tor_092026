"""The discovery helpers on their own: month calendars, data feeds, calendar files, sitemaps, ticket sellers."""
import datetime as dt

import lenses as L

TODAY = dt.date(2026, 9, 25)


def test_every_month_of_the_season_from_one_month_page():
    for url, first, last in [
        ("https://opera.com.ua/afisha?month=01-10-2026&type=98",
         "https://opera.com.ua/afisha?month=01-11-2026&type=98", "https://opera.com.ua/afisha?month=01-09-2027&type=98"),
        ("https://operanb.ro/calendar/?luna=10&anul=2026",
         "https://operanb.ro/calendar/?luna=11&anul=2026", "https://operanb.ro/calendar/?luna=09&anul=2027"),
        ("https://x.org/spielplan/2026/10/", "https://x.org/spielplan/2026/11/", "https://x.org/spielplan/2027/09/"),
        ("https://x.org/agenda/2026-10", "https://x.org/agenda/2026-11", "https://x.org/agenda/2027-09"),
        ("https://x.org/cal?date=2026-10-01&cat=dance", "https://x.org/cal?date=2026-11-01&cat=dance",
         "https://x.org/cal?date=2027-09-01&cat=dance"),
        ("https://x.org/cal?m=10&y=2026", "https://x.org/cal?m=11&y=2026", "https://x.org/cal?m=09&y=2027"),
    ]:
        months = L.month_series(url, TODAY, 12)
        assert months[0] == first and months[-1] == last and len(months) == 11, url


def test_ordinary_pagination_is_not_mistaken_for_months():
    assert L.month_series("https://x.org/events?page=2", TODAY) == []
    assert L.month_series("https://x.org/programme/giselle-2026", TODAY) == []


def test_feed_for_one_week_is_asked_for_the_season():
    assert L.widen_feed_url("https://api.x.org/events?from=2026-09-21&to=2026-09-28&lang=en", TODAY) == \
        "https://api.x.org/events?from=2026-09-25&to=2027-09-25&lang=en"
    assert L.widen_feed_url("https://x.org/api?start=21.09.2026&end=28.09.2026", TODAY) == \
        "https://x.org/api?start=25.09.2026&end=25.09.2027"
    assert L.widen_feed_url("https://x.org/api?page=2", TODAY) is None


def test_calendar_file_events():
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Rituals\r\nDTSTART;TZID=Europe/Paris:20261203T193000\r\n"
           "LOCATION:Palais Garnier\\, Paris\r\nURL:https://x.org/rituals\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\n"
           "SUMMARY:Rituals (matinee)\r\nDTSTART:20261206T133000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    ev = L.parse_ics(ics)
    assert [e["name"] for e in ev] == ["Rituals", "Rituals (matinee)"]
    assert ev[0]["startDate"].startswith("2026-12-03T19:30") and ev[0]["location"]["name"] == "Palais Garnier, Paris"
    assert ev[1]["startDate"] == "2026-12-06T13:30:00+00:00"


def test_sitemap_keeps_this_seasons_event_pages_only():
    xml = """<urlset>
      <url><loc>https://x.org/en/season-26-27/ballet/joyaux</loc><lastmod>2026-08-01</lastmod></url>
      <url><loc>https://x.org/en/season-24-25/ballet/giselle</loc></url>
      <url><loc>https://x.org/spielplan/2024-2025/nussknacker</loc></url>
      <url><loc>https://x.org/archive/2023/swan-lake</loc></url>
      <url><loc>https://x.org/news/new-director</loc></url>
      <url><loc>https://x.org/productions/rituals</loc><lastmod>2026-09-01</lastmod></url>
      <url><loc>https://x.org/productions/old-show</loc><lastmod>2023-01-01</lastmod></url>
      <url><loc>https://x.org/about-us</loc></url></urlset>"""
    pages, kids = L.parse_sitemap(xml)
    assert len(pages) == 8 and kids == []
    keep = [u for u, m in pages if L.event_like(u, m, TODAY)]
    assert keep == ["https://x.org/en/season-26-27/ballet/joyaux", "https://x.org/productions/rituals"]
    _, kids = L.parse_sitemap("<sitemapindex><sitemap><loc>https://x.org/sitemap-events.xml</loc></sitemap></sitemapindex>")
    assert kids == ["https://x.org/sitemap-events.xml"]


def test_ticket_sellers_are_recognised_but_venue_booking_systems_are_not():
    for u in ("https://www.ticketmaster.co.uk/x", "https://www.eventim.de/x", "https://www.seetickets.com/x",
              "https://www.fnacspectacles.com/x", "https://www.atgtickets.com/x"):
        assert L.is_ticket_seller(u), u
    for u in ("https://billetterie.operadeparis.fr/x", "https://tickets.sadlerswells.com/x", "https://www.matrix.de/x"):
        assert not L.is_ticket_seller(u), u
    assert L.site_family("https://billetterie.operadeparis.fr/a") == "operadeparis.fr"
    assert L.site_family("https://www.ballet.org.uk/x") == "ballet.org.uk"


def test_months_in_addresses_are_not_mistaken_for_seasons():
    for url, keep in [("https://x.org/programme/2025-2026/giselle", False), ("https://x.org/programme/2026-2027/giselle", True),
                      ("https://x.org/spielplan/2026/10/giselle", True), ("https://x.org/saison-25-26/x", False),
                      ("https://x.org/saison-26-27/x", True), ("https://x.org/event/2025-12-01-x", False),
                      ("https://x.org/event/2026-12-01-x", True)]:
        assert L.event_like(url, "", TODAY) == keep, url
