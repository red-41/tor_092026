"""Discovery helpers ("lenses") that find dates a schedule page does not show directly.

All pure functions (no browser, no network), so each can be tested on its own:
- month_series:   a calendar paged by month -> the address of every month up to the season's end
- widen_feed_url: a data feed asked for one week or month -> the same feed asked for the whole season
- parse_ics:      "add to calendar" files -> events
- parse_sitemap / event_like: a site's sitemap.xml -> its event and production pages
- is_ticket_seller: third-party ticket sellers, never used (the site only uses venues' and companies' own pages)
"""
import datetime as dt
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# ----------------------------------------------------------------------------- third-party ticket sellers
TICKET_SELLERS = (
    "ticketmaster.", "eventim.", "seetickets.", "fnacspectacles.", "ticketone.", "entradas.com", "ticketcorner.",
    "reservix.", "ticketswap.", "viagogo.", "stubhub.", "ticketportal.", "billetto.", "eventbrite.", "songkick.",
    "bandsintown.", "todaytix.", "atgtickets.", "ticketline.", "giletjaune.", "digitick.", "billetreduc.",
    "ticketea.", "eticket.", "ticketsource.", "ticketweb.", "axs.com", "livenation.", "gigantic.",
    "skiddle.", "fever", "ticketpro.", "biletix.", "kupbilecik.", "ebilet.", "goout.", "ticketstream.",
    "oeticket.", "myticket.", "adticket.", "ticketonline.", "tix.", "ticket.fi", "lippu.fi", "tiketti.",
    "billetlugen.", "billettservice.", "ticnet.", "ticketmaster", "ticket-online.", "vivaticket.", "boxol.",
    "kartenhaus.", "tickets.com", "teaterbilletter.",
)

# Resellers and listing sites that are not the venue's own box office: never used as a ticket link.
AGGREGATORS = ("teaterbilletter.", "viagogo.", "stubhub.", "ticketswap.", "songkick.", "bandsintown.", "todaytix.",
               "billetreduc.", "fever", "gigsberg.", "seatgeek.", "ticketnetwork.")


def is_aggregator(url: str | None) -> bool:
    host = (urlparse(url or "").netloc or "").lower()
    return any(t in host for t in AGGREGATORS)


def is_ticket_seller(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(t in host for t in TICKET_SELLERS)


def site_family(url: str) -> str:
    """'billetterie.operadeparis.fr' and 'www.operadeparis.fr' -> 'operadeparis.fr' (also handles co.uk and the like)."""
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac", "net", "gob", "gouv", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ----------------------------------------------------------------------------- calendars paged by month
_MONTH_KEYS = r"(?:month|monat|mois|mes|maand|mese|luna|miesiac|mesic|kuukausi|maaned|manad|m)"
_YEAR_KEYS = r"(?:year|jahr|annee|anno|ano|jaar|anul|rok|vuosi|ar|y)"


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    k = year * 12 + (month - 1) + n
    return k // 12, k % 12 + 1


def month_series(url: str, today: dt.date, months_ahead: int = 12) -> list[str]:
    """If `url` is one month of a calendar, the addresses of the following months up to `months_ahead` from today.
    Understood: ?month=2026-11, ?month=01-11-2026, ?date=2026-11-01, /2026/11/, /2026-11, ?luna=11&anul=2026,
    ?m=11&y=2026. Anything else: [] (the collector then just follows the site's own 'next' link)."""
    p = urlparse(url)
    q = parse_qsl(p.query, keep_blank_values=True)
    last = _add_months(today.year, today.month, months_ahead)

    def series(year, month, make):
        out, y, m = [], year, month
        for _ in range(24):
            y, m = _add_months(y, m, 1)
            if (y, m) > last:
                break
            out.append(make(y, m))
        return out

    # separate month and year parameters
    mi = next((i for i, (k, v) in enumerate(q) if re.fullmatch(_MONTH_KEYS, k, re.I) and re.fullmatch(r"\d{1,2}", v)), None)
    yi = next((i for i, (k, v) in enumerate(q) if re.fullmatch(_YEAR_KEYS, k, re.I) and re.fullmatch(r"20\d\d", v)), None)
    if mi is not None and yi is not None and 1 <= int(q[mi][1]) <= 12:
        width = len(q[mi][1])

        def make(y, m):
            qq = list(q)
            qq[mi] = (qq[mi][0], str(m).zfill(width))
            qq[yi] = (qq[yi][0], str(y))
            return urlunparse(p._replace(query=urlencode(qq, safe="-/:")))
        return series(int(q[yi][1]), int(q[mi][1]), make)

    # one parameter holding a date or a month
    forms = [
        (r"(20\d\d)-(\d{2})-(\d{2})", lambda y, m, d: f"{y}-{m:02d}-01"),
        (r"(\d{2})-(\d{2})-(20\d\d)", lambda y, m, d: f"01-{m:02d}-{y}"),
        (r"(\d{2})\.(\d{2})\.(20\d\d)", lambda y, m, d: f"01.{m:02d}.{y}"),
        (r"(20\d\d)-(\d{2})", lambda y, m, d: f"{y}-{m:02d}"),
        (r"(\d{2})-(20\d\d)", lambda y, m, d: f"{m:02d}-{y}"),
        (r"(20\d\d)(\d{2})", lambda y, m, d: f"{y}{m:02d}"),
    ]
    for i, (k, v) in enumerate(q):
        for rx, fmt in forms:
            mt = re.fullmatch(rx, v)
            if not mt:
                continue
            g = mt.groups()
            if rx.startswith("(20"):
                y, m = int(g[0]), int(g[1])
            elif len(g) == 3:
                y, m = int(g[2]), int(g[1])
            else:
                y, m = int(g[1]), int(g[0])
            if not 1 <= m <= 12:
                continue

            def make(yy, mm, i=i, k=k, fmt=fmt):
                qq = list(q)
                qq[i] = (k, fmt(yy, mm, 1))
                return urlunparse(p._replace(query=urlencode(qq, safe="-/:.")))
            return series(y, m, make)

    # the month in the path: /2026/11/ or /2026-11
    mt = re.search(r"/(20\d\d)([/-])(\d{1,2})(?=/|$)", p.path)
    if mt and 1 <= int(mt.group(3)) <= 12:
        width = len(mt.group(3))

        def make(y, m):
            path = p.path[: mt.start()] + f"/{y}{mt.group(2)}{str(m).zfill(width)}" + p.path[mt.end():]
            return urlunparse(p._replace(path=path))
        return series(int(mt.group(1)), int(mt.group(3)), make)
    return []


# ----------------------------------------------------------------------------- data feeds asked for a short window
_FROM_KEYS = r"(?:from|start|startdate|start_date|datefrom|date_from|fromdate|from_date|begin|von|du|desde|da|van|od|after|since|mindate|min_date)"
_TO_KEYS = r"(?:to|end|enddate|end_date|dateto|date_to|todate|to_date|until|bis|au|hasta|a|tot|do|before|maxdate|max_date)"


def _same_format(sample: str, d: dt.date) -> str | None:
    if re.fullmatch(r"20\d\d-\d{2}-\d{2}", sample):
        return d.isoformat()
    if re.fullmatch(r"20\d\d-\d{2}-\d{2}T[\d:.]+Z?", sample):
        return d.isoformat() + sample[10:]
    if re.fullmatch(r"\d{2}\.\d{2}\.20\d\d", sample):
        return d.strftime("%d.%m.%Y")
    if re.fullmatch(r"\d{2}/\d{2}/20\d\d", sample):
        return d.strftime("%d/%m/%Y")
    if re.fullmatch(r"20\d\d\d{4}", sample):
        return d.strftime("%Y%m%d")
    if re.fullmatch(r"1\d{9}", sample):
        return str(int(dt.datetime.combine(d, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp()))
    if re.fullmatch(r"1\d{12}", sample):
        return str(int(dt.datetime.combine(d, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp()) * 1000)
    return None


def widen_feed_url(url: str, today: dt.date, days: int = 365) -> str | None:
    """A feed asked for a date window (from=...&to=...) -> the same feed from today to `days` ahead. None if not a
    date-window feed or already at least that wide."""
    p = urlparse(url)
    q = parse_qsl(p.query, keep_blank_values=True)
    fi = next((i for i, (k, v) in enumerate(q) if re.fullmatch(_FROM_KEYS, k.replace("-", "").lower()) and _same_format(v, today)), None)
    ti = next((i for i, (k, v) in enumerate(q) if re.fullmatch(_TO_KEYS, k.replace("-", "").lower()) and _same_format(v, today)), None)
    if fi is None or ti is None:
        return None
    new_to = _same_format(q[ti][1], today + dt.timedelta(days=days))
    new_from = _same_format(q[fi][1], today)
    if q[ti][1] == new_to:
        return None
    qq = list(q)
    qq[fi] = (qq[fi][0], new_from)
    qq[ti] = (qq[ti][0], new_to)
    return urlunparse(p._replace(query=urlencode(qq, safe="-:.")))


DATE_IN_TEXT = re.compile(r"(20\d\d-[01]\d-[0-3]\d|[0-3]?\d[./][01]?\d[./]20\d\d)")


def date_count(text: str) -> int:
    return len(DATE_IN_TEXT.findall(text or ""))


# ----------------------------------------------------------------------------- "add to calendar" files
def _ics_time(value: str, params: str) -> str | None:
    v = value.strip()
    try:
        if re.fullmatch(r"\d{8}", v):
            return dt.datetime.strptime(v, "%Y%m%d").date().isoformat()
        if re.fullmatch(r"\d{8}T\d{6}Z", v):
            return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).isoformat()
        if re.fullmatch(r"\d{8}T\d{4,6}", v):
            d = dt.datetime.strptime(v[:13], "%Y%m%dT%H%M")
            tz = re.search(r"TZID=([^;:]+)", params or "")
            return d.isoformat() + (f" ({tz.group(1)})" if tz else "")
    except ValueError:
        return None
    return None


def parse_ics(text: str) -> list[dict]:
    """VEVENTs of an .ics file as schema.org-like events (name, startDate, location, url)."""
    lines, out, cur = [], [], None
    for raw in (text or "").splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw.rstrip("\r"))
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("startDate"):
                out.append({"@type": "Event", "_from": "calendar file", **cur})
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            value = value.replace("\\,", ",").replace("\\n", " ").replace("\\;", ";")
            if name == "SUMMARY":
                cur["name"] = value
            elif name == "DTSTART":
                cur["startDate"] = _ics_time(value, params)
            elif name == "LOCATION":
                cur["location"] = {"name": value}
            elif name == "URL":
                cur["url"] = value
            elif name == "DESCRIPTION":
                cur["description"] = value[:300]
    return out


# ----------------------------------------------------------------------------- sitemaps
EVENT_PATH = re.compile(
    r"(event|evenement|veranstaltung|spielplan|production|produktion|spectacle|voorstelling|forestilling|"
    r"f[oö]rest[aä]llning|programm|program|agenda|kalender|calendar|calendrier|show|performance|st[uü]ck|obra|"
    r"espectaculo|espect[aá]culo|spettacolo|ballet|ballett|balet|danse|dance|tanz|dans|danza|dan[cç]a|taniec|"
    r"tanec|tanssi|repertoire|repertoar|season|saison|seizoen|stagione|temporada|sezon|whats-on|what-s-on)", re.I)
OLD_SEASON = re.compile(r"(?<!\d)(20\d\d)(?:[-/_]?(20)?(\d\d))?(?!\d)")


def parse_sitemap(xml: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(pages [(url, lastmod)], child sitemaps) of a sitemap or sitemap index."""
    children = re.findall(r"<sitemap>\s*<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.I)
    pages = []
    for block in re.findall(r"<url>(.*?)</url>", xml or "", re.I | re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block, re.I)
        mod = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", block, re.I)
        if loc:
            pages.append((loc.group(1).replace("&amp;", "&"), mod.group(1)[:10] if mod else ""))
    return pages, [c.replace("&amp;", "&") for c in children]


def event_like(url: str, lastmod: str, today: dt.date) -> bool:
    """A page of an event or production in this or next season (not the archive, not news or people)."""
    path = urlparse(url).path.lower()
    if not EVENT_PATH.search(path) or re.search(r"(archiv|archive|news|nieuws|actualit|press|presse|blog|artist|"
                                                r"kuenstler|people|team|job|vacature|education|workshop|kurs|class)", path):
        return False
    oldest_current = today.year - (1 if today.month < 8 else 0)     # the season that started last August or later
    for m in OLD_SEASON.finditer(path):
        year = int(m.group(1))
        if m.group(3) and int(m.group(3)) == (year + 1) % 100:   # a season 2025-26 / 2025-2026 ended when the next began
            if int((m.group(2) or "20") + m.group(3)) <= oldest_current:
                return False
        elif year < oldest_current:
            return False
    for m in re.finditer(r"(?<!\d)(\d\d)[-_/](\d\d)(?!\d)", path):        # short seasons: 24-25, 25_26
        a, b = int(m.group(1)), int(m.group(2))
        if b == (a + 1) % 100 and 2000 + b <= oldest_current:
            return False
    if lastmod:
        try:
            if dt.date.fromisoformat(lastmod) < today - dt.timedelta(days=540):
                return False
        except ValueError:
            pass
    return True
