"""Browser: open a page like a normal visitor, deal with cookie banners, expand lists,
and return its text, links and event data. Also tells blocked pages apart from empty ones.

What it does NOT do: solve captchas, fake a human, or rotate IP addresses. If a site
actively refuses automated visitors, the page is reported as blocked and skipped.
"""
import datetime as dt
import gzip
import io
import json
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import config
import lenses

# ---------------------------------------------------------------- cookie banners
# Consent managers seen on European venue sites: their "reject" / "only necessary" buttons.
REJECT_SELECTORS = [
    "#onetrust-reject-all-handler", ".ot-pc-refuse-all-handler",                       # OneTrust
    "#CybotCookiebotDialogBodyButtonDecline",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",                       # Cookiebot
    "[data-testid='uc-deny-all-button']",                                             # Usercentrics
    "#didomi-notice-disagree-button", ".didomi-continue-without-agreeing",            # Didomi
    "#axeptio_btn_dismiss",                                                           # Axeptio
    "#truste-consent-required",                                                       # TrustArc
    ".cmplz-deny", ".cmplz-btn.cmplz-deny",                                           # Complianz
    ".iubenda-cs-reject-btn",                                                         # iubenda
    ".cky-btn-reject",                                                                # CookieYes
    "._brlbs-refuse-btn", "._brlbs-refuse", "a[data-cookie-refuse]",                  # Borlabs
    ".cm-btn-decline", ".cn-decline",                                                 # Klaro
    ".osano-cm-denyAll",                                                              # Osano
    "#cn-refuse-cookie",                                                              # Cookie Notice
    "#tarteaucitronAllDenied2", ".tarteaucitronDeny",                                 # tarteaucitron
    "#cookiescript_reject",                                                           # CookieScript
    "[data-tid='banner-decline']",                                                    # Termly
    "#ccc-reject-settings", "#ccc-notify-reject",                                     # Civic UK
    ".cmpboxbtnno",                                                                   # consentmanager
    ".moove-gdpr-infobar-reject-btn",                                                 # GDPR Cookie Compliance
    "#cookie_action_close_header_reject",                                             # CookieLawInfo
    ".coi-banner__decline", "#declineButton",                                         # Cookie Information
    "#ppms_cm_reject-all",                                                            # Piwik PRO
    ".cm__btn[data-role='necessary']", "button[data-role='necessary']",               # cookieconsent v3
    ".cc-deny", "button.cookie-reject",
]
BANNER_SELECTORS = [
    "#onetrust-banner-sdk", "#onetrust-consent-sdk", "#CybotCookiebotDialog", "#usercentrics-root",
    "#didomi-host", ".cc-window", "#cookie-law-info-bar", "#cmplz-cookiebanner-container",
    ".cky-consent-container", "#axeptio_overlay", "#truste-consent-track", ".qc-cmp2-container",
    "iframe[id^='sp_message_iframe']", "#cookiescript_injected", "#iubenda-cs-banner",
    "#tarteaucitronRoot", "#BorlabsCookieBox", "#coiOverlay", "#cmpbox", "#ppms_cm_consent_popup_overlay",
]
REJECT_WORDS = re.compile(
    r"(reject all|reject|refuse|decline|deny|only necessary|necessary only|essential only|use necessary|"
    r"only essential|continue without|without accepting|"
    r"alle ablehnen|ablehnen|nur notwendige|nur essenzielle|"
    r"tout refuser|refuser|continuer sans accepter|"
    r"weigeren|alles weigeren|alleen noodzakelijk|"
    r"rifiuta|rifiuta tutto|solo necessari|rechazar|rechazar todo|solo necesarias|"
    r"avvisa|avvisa alla|endast nödvändiga|afvis|afvis alle|kun nødvendige|kun nødvendige cookies|"
    r"avslå|bare nødvendige|kieltäydy|hylkää|vain välttämättömät|"
    r"odmítnout|odmítnout vše|odrzuć|odrzuć wszystkie|elutasít|recusar|rejeitar|nej tack)",
    re.I,
)
ACCEPT_WORDS = re.compile(
    r"^\s*(accept all|accept all cookies|accept|allow all|allow all cookies|agree|i agree|agree and close|got it|ok|okay|"
    r"alle akzeptieren|akzeptieren|alle zulassen|einverstanden|tout accepter|accepter|j'accepte|accepter et fermer|"
    r"alles accepteren|accepteren|akkoord|accetta|accetta tutto|aceptar|aceptar todo|acceptera|acceptera alla|"
    r"accepter alle|accepter alle cookies|tillad alle|godta alle|hyväksy|hyväksy kaikki|souhlasím|přijmout vše|"
    r"akceptuj|akceptuję|zaakceptuj wszystkie|elfogadom|összes elfogadása|aceitar|aceitar todos)\s*$",
    re.I,
)
COOKIE_WORDS = r"(cookie|consent|gdpr|datenschutz|privacy|privacidad|confidentialit|tracking|samtykke|toestemming)"

# JS: count visible fixed/sticky boxes that talk about cookies; optionally remove them and unlock scrolling.
BANNER_JS = """(remove) => {
  const re = new RegExp('%s', 'i');
  const named = /(cookie|consent|gdpr|\\bcmp)/i;
  const out = [];
  for (const e of document.querySelectorAll('body *')) {
    const byName = named.test(e.id + ' ' + (typeof e.className === 'string' ? e.className : ''));
    const s = getComputedStyle(e);
    const fixed = s.position === 'fixed' || s.position === 'sticky';
    if (!byName && !fixed) continue;
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
    const r = e.getBoundingClientRect();
    if (r.width < 200 || r.height < (byName ? 10 : 30)) continue;
    const txt = (e.innerText || '');
    if (txt.length > 4000) continue;                       // never touch something holding real content
    if (byName && !fixed && !e.querySelector('button, a, [role=button]')) continue;
    const big = r.width * r.height > 0.5 * innerWidth * innerHeight;
    if ((fixed && re.test(txt)) || byName || (remove && fixed && big && txt.trim().length < 20)) out.push(e);
  }
  if (remove && out.length) {
    out.forEach(e => e.remove());
    for (const el of [document.documentElement, document.body]) {
      el.style.setProperty('overflow', 'auto', 'important');
      el.style.setProperty('position', 'static', 'important');
    }
  }
  return out.length;
}""" % COOKIE_WORDS

LOAD_MORE = re.compile(r"(load more|show more|more events|see more|view more|all dates|mehr anzeigen|weitere termine|mehr laden|"
                       r"voir plus|afficher plus|plus de dates|meer laden|toon meer|meer tonen|toon volgende|meer weergeven|"
                       r"mostra altri|mostra di più|carica altri|ver más|cargar más|pokaż więcej|näytä lisää|vis flere|"
                       r"se flere|visa fler|načíst další|zobrazit další|zobrazit více|načíst více|további|mais eventos)", re.I)
# In-page buttons and tabs that reveal a production's individual dates (never links that leave the page).
DATE_BUTTONS = re.compile(
    r"^\s*(dates?|all dates|show dates|see (?:all )?dates|view (?:all )?dates|more dates|dates (?:and|&) (?:tickets|times|prices)|"
    r"(?:all |show |view |see )?(?:sessions|performances)(?: and prices| & prices)?|showtimes|times (?:and|&) tickets|"
    r"séances|voir les (?:dates|séances)|toutes les dates|les dates|dates et (?:tarifs|horaires)|voir plus de dates|"
    r"termine|alle termine|termine (?:und|&) (?:tickets|karten)|spieltermine|vorstellungen|"
    r"fechas|todas las fechas|ver fechas|funciones|date e orari|tutte le date|date|repliche|"
    r"speeldata|alle speeldata|speeldagen|voorstellingen|forestillinger|alle forestillinger|föreställningar|spilledatoer|"
    r"esitykset|näytökset|terminy|wszystkie terminy|termíny|všechny termíny|reprízy|előadások|időpontok|datas|sessões)"
    r"\s*[›»>→+\-]*\s*$", re.I)
# Hosts whose background data is never about the programme.
NOT_PROGRAMME_HOSTS = re.compile(r"(google|gstatic|facebook|doubleclick|hotjar|cookiebot|onetrust|usercentrics|didomi|"
                                 r"sentry|segment|matomo|piwik|clarity|tiktok|linkedin|twitter|cloudflareinsights|"
                                 r"newrelic|nr-data|hubspot|mailchimp|recaptcha|trustpilot)", re.I)

# ---------------------------------------------------------------- bot walls
BLOCK_TITLE = re.compile(r"(just a moment|attention required|access denied|403 forbidden|forbidden|"
                         r"you have been blocked|request rejected|pardon our interruption|are you a robot|"
                         r"security check|ddos-guard|checking your browser|please wait\.\.\.|bot verification)", re.I)
BLOCK_MARKERS = [
    ("cloudflare", re.compile(r"(challenges\.cloudflare\.com|cf-chl|cf_chl|checking your browser|verify you are human|"
                              r"enable javascript and cookies to continue|sorry, you have been blocked|ray id)", re.I)),
    ("imperva", re.compile(r"(incapsula|_incapsula_resource|request unsuccessful|pardon our interruption)", re.I)),
    ("datadome", re.compile(r"(captcha-delivery\.com|datadome)", re.I)),
    ("perimeterx", re.compile(r"(px-captcha|perimeterx|press (?:and|&) hold)", re.I)),
    ("akamai", re.compile(r"(errors\.edgesuite\.net|reference #\d+\.[0-9a-f]+)", re.I)),
    ("captcha", re.compile(r"(g-recaptcha|h-captcha|hcaptcha\.com/1/api|are you a robot|i am not a robot|captcha)", re.I)),
    ("access denied", re.compile(r"(access denied|access to this page has been denied|request blocked|not authorized)", re.I)),
]
WAITING_TITLE = re.compile(r"(just a moment|checking your browser|please wait|one moment|un instant|einen moment|"
                           r"ddos-guard|security check)", re.I)
THIN_CHARS = 1500           # below this, a page is suspicious (bot wall, JS not loaded, consent wall)

EVENT_TYPES = {"Event", "TheaterEvent", "DanceEvent", "MusicEvent", "Festival", "ScreeningEvent", "EventSeries"}


@dataclass
class Snapshot:
    url: str
    final_url: str
    title: str = ""
    text: str = ""
    links: list = field(default_factory=list)       # [(anchor text, absolute url)]
    events: list = field(default_factory=list)      # schema.org events found in the page
    og_image: str = ""
    error: str = ""
    status: int = 0             # HTTP status of the page
    blocked: str = ""           # which bot wall stopped us ("cloudflare", "captcha", "HTTP 403"...), empty if none
    cookie: str = "none"        # none | refused | accepted | hidden | banner remains
    screenshot: str = ""        # file path, when screenshots are on
    feeds: list = field(default_factory=list)       # [{"url", "text"}] data (JSON) the page loaded in the background
    sitemap: list = field(default_factory=list)     # event/production addresses from the site's sitemap.xml


def _flatten_ld(node, out):
    if isinstance(node, list):
        for n in node:
            _flatten_ld(n, out)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t in EVENT_TYPES for t in types if isinstance(t, str)):
            out.append(node)
        for key in ("@graph", "subEvent", "event", "itemListElement", "item"):
            if key in node:
                _flatten_ld(node[key], out)


def detect_block(status: int, title: str, text: str, html: str) -> str:
    """Name of the bot wall on this page, or '' if the page looks like real content."""
    if len(text) >= THIN_CHARS * 2:
        return ""                                   # plenty of real content: a footer captcha does not count
    thin = len(text) < THIN_CHARS
    if BLOCK_TITLE.search(title or "") and thin:
        for name, rx in BLOCK_MARKERS:
            if rx.search(html) or rx.search(text):
                return name
        return "bot wall"
    if thin:
        for name, rx in BLOCK_MARKERS:
            if name == "captcha" and status < 400 and len(text) > 300:
                continue                            # a newsletter form with reCAPTCHA is not a wall
            if rx.search(html[:300000]) or rx.search(text):
                return name
    if status in (401, 403, 429, 503) and thin:
        return f"HTTP {status}"
    return ""


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower().split("//")[-1])[:90].strip("-")


class Robots:
    def __init__(self):
        self._cache = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return True
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = httpx.get(root + "/robots.txt", timeout=15, follow_redirects=True,
                              headers={"User-Agent": config.BOT_UA}, verify=False)
                rp.parse(r.text.splitlines() if r.status_code == 200 and "html" not in r.headers.get("content-type", "") else [])
            except Exception:
                rp.parse([])
            self._cache[root] = rp
        return self._cache[root].can_fetch(config.BOT_NAME, url)

    def sitemaps(self, url: str) -> list[str]:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        self.allowed(root + "/")
        try:
            return list(self._cache[root].site_maps() or [])
        except Exception:
            return []


class Browser:
    """shots: None (no screenshots), 'problems' (only blocked/empty pages) or 'always'."""

    def __init__(self, shots_dir: str | None = None, shots: str | None = None):
        self.shots_dir = shots_dir
        self.shots = shots if shots_dir else None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._launch()
        self.robots = Robots()
        self._sitemaps = {}
        if self.shots_dir:
            import os
            os.makedirs(self.shots_dir, exist_ok=True)
        return self

    def _launch(self):
        try:        # full Chromium in new headless mode behaves like a normal browser
            self._browser = self._pw.chromium.launch(headless=True, channel="chromium")
        except Exception:
            self._browser = self._pw.chromium.launch(headless=True)
        self.ua = ua = config.USER_AGENT or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{self._browser.version.split('.')[0]}.0.0.0 Safari/537.36")
        self._ctx = self._browser.new_context(
            user_agent=ua, locale="en-GB", viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"}, ignore_https_errors=True)
        self._dead = False
        self.restarts = getattr(self, "restarts", 0)

    def _freeze(self):
        """Watchdog: a page is still loading after PAGE_HARD_LIMIT_S. A stuck script can block the browser for
        ever, so the browser is stopped (the waiting call then fails) and load() starts a fresh one."""
        self._dead = True
        print(f"watchdog: a page froze for {config.PAGE_HARD_LIMIT_S}s; restarting the browser", flush=True)
        try:
            import psutil
            for p in psutil.Process().children(recursive=True):
                try:
                    name = p.name().lower()
                    if "chrom" in name or "headless_shell" in name:
                        p.kill()
                except Exception:
                    pass
        except Exception as e:
            print(f"watchdog: could not stop the browser ({type(e).__name__})", flush=True)

    def _restart(self):
        for closer in (lambda: self._ctx.close(), lambda: self._browser.close()):
            try:
                closer()
            except Exception:
                pass
        self.restarts += 1
        self._launch()

    def __exit__(self, *exc):
        for closer in (lambda: self._ctx.close(), lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    # -------------------------------------------------------- cookie banners
    def _click_first(self, frame, selectors) -> bool:
        for sel in selectors:
            try:
                el = frame.locator(sel).first
                if el.count() and el.is_visible(timeout=200):
                    el.click(timeout=1500)
                    return True
            except Exception:
                pass
        return False

    def _click_words(self, frame, words) -> bool:
        for role in ("button", "link"):
            try:
                el = frame.get_by_role(role, name=words).first
                if el.count() and el.is_visible(timeout=300):
                    el.click(timeout=1500)
                    return True
            except Exception:
                pass
        return False

    def _banner_present(self, page) -> bool:
        for sel in BANNER_SELECTORS:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=100):
                    return True
            except Exception:
                pass
        try:
            return page.evaluate(BANNER_JS, False) > 0
        except Exception:
            return False

    def _handle_cookies(self, page) -> str:
        """Refuse if possible. Accept only when the site shows nothing until you do.
        Anything still covering the page is removed from view."""
        if not self._banner_present(page):
            return "none"
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        for f in frames:
            if self._click_first(f, REJECT_SELECTORS) or self._click_words(f, REJECT_WORDS):
                page.wait_for_timeout(800)
                if not self._banner_present(page):
                    return "refused"
                break
        # Consent wall: content hidden until you agree. Accept is the only way in.
        if len(self._text(page)) < THIN_CHARS:
            for f in frames:
                if self._click_words(f, ACCEPT_WORDS):
                    page.wait_for_timeout(2000)
                    return "accepted"
        try:
            if page.evaluate(BANNER_JS, True) > 0:
                return "hidden"
        except Exception:
            pass
        return "banner remains" if self._banner_present(page) else "refused"

    # -------------------------------------------------------- page helpers
    @staticmethod
    def _text(page) -> str:
        try:
            t = page.inner_text("body", timeout=5000)
        except Exception:
            return ""
        return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", t)).strip()

    def _wait_out_check(self, page):
        """Some sites show a few seconds of 'Checking your browser' before the page. Wait for it to finish."""
        for _ in range(int(config.CHALLENGE_WAIT_S)):
            try:
                if not WAITING_TITLE.search(page.title()):
                    return
            except Exception:
                pass
            page.wait_for_timeout(1000)

    def _reveal_dates(self, page):
        """Open collapsed sections and click in-page 'Dates' / 'Sessions' / 'Termine' buttons and tabs."""
        try:
            page.evaluate("() => document.querySelectorAll('details:not([open])').forEach(d => d.open = true)")
        except Exception:
            pass
        clicked = 0
        for role in ("button", "tab"):
            try:
                loc = page.get_by_role(role, name=DATE_BUTTONS)
                for i in range(min(loc.count(), 4)):
                    if clicked >= 4:
                        return
                    el = loc.nth(i)
                    if not el.is_visible(timeout=300):
                        continue
                    before = page.url
                    el.click(timeout=2000)
                    page.wait_for_timeout(1200)
                    clicked += 1
                    if page.url != before and lenses.is_ticket_seller(page.url):
                        page.go_back(timeout=15000)       # never read a ticket seller's page
            except Exception:
                continue

    def _expand(self, page):
        self._reveal_dates(page)
        for _ in range(6):
            try:
                btn = page.get_by_role("button", name=LOAD_MORE).first
                if not btn.count() or not btn.is_visible(timeout=500):
                    btn = page.get_by_role("link", name=LOAD_MORE).first      # only in-page links, never navigation
                    if (not btn.count() or not btn.is_visible(timeout=300)
                            or (btn.get_attribute("href") or "#").strip() not in ("#", "", "javascript:void(0)", "javascript:;")):
                        break
                btn.click(timeout=2000)
                page.wait_for_timeout(1500)
            except Exception:
                break
        for _ in range(5):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(400)

    def _shot(self, page, snap, problem: bool):
        if not self.shots or (self.shots == "problems" and not problem):
            return
        path = f"{self.shots_dir}/{_slug(snap.url)}.jpg"
        try:
            page.screenshot(path=path, type="jpeg", quality=55, timeout=10000)
            snap.screenshot = path
        except Exception:
            pass

    def _load_pdf(self, snap: Snapshot) -> Snapshot:
        try:
            from pypdf import PdfReader
            r = httpx.get(snap.url, timeout=60, follow_redirects=True, verify=False,
                          headers={"User-Agent": self.ua})
            r.raise_for_status()
            reader = PdfReader(io.BytesIO(r.content))
            snap.text = "\n".join((p.extract_text() or "") for p in reader.pages[:40]).strip()
            snap.title, snap.final_url, snap.status = "PDF", str(r.url), r.status_code
        except Exception as e:
            snap.error = f"PDF: {type(e).__name__}: {str(e)[:150]}"
        return snap

    # -------------------------------------------------------- lenses: background data, calendar files, sitemaps
    def _feeds(self, page, captured) -> list[dict]:
        """JSON the page loaded that holds dates (a booking widget's list of performances, a calendar's month).
        A feed asked for a short window (from=...&to=...) is asked again for the whole season."""
        found = []
        for resp in captured:
            try:
                body = resp.text()
            except Exception:
                continue
            n = lenses.date_count(body)
            if n >= 2 and len(body) < 3_000_000:
                found.append((n, resp.url, body))
        found.sort(key=lambda x: -x[0])
        out = []
        for n, u, body in found[:3]:
            wide = lenses.widen_feed_url(u, dt.date.today())
            if wide and self.robots.allowed(wide):
                try:
                    r = page.request.get(wide, timeout=20000)
                    if r.ok and lenses.date_count(r.text()) > n:
                        u, body = wide, r.text()
                except Exception:
                    pass
            try:
                body = json.dumps(json.loads(body), ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass
            out.append({"url": u, "text": body[: config.FEED_TEXT_LIMIT]})
        return out

    def _calendar_files(self, hrefs: list[str]) -> list[dict]:
        """Events from the page's 'add to calendar' (.ics) files; no model needed to read them."""
        events = []
        ics = [h for h in hrefs if re.search(r"(\.ics($|\?)|[?&/]ical\b|format=ical|/ics/)", h, re.I)]
        for h in list(dict.fromkeys(ics))[:3]:
            if not self.robots.allowed(h):
                continue
            try:
                r = httpx.get(h, timeout=20, follow_redirects=True, verify=False, headers={"User-Agent": self.ua})
                if r.status_code == 200 and "BEGIN:VCALENDAR" in r.text[:2000]:
                    events += lenses.parse_ics(r.text[:500_000])[:200]
            except Exception:
                continue
        return events

    def sitemap(self, url: str, limit: int = 200) -> list[str]:
        """Event and production pages of this site listed in its sitemap.xml (newest first). Cached per site."""
        fam = lenses.site_family(url)
        if fam in self._sitemaps:
            return self._sitemaps[fam]
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        queue = self.robots.sitemaps(url) or [root + "/sitemap.xml", root + "/sitemap_index.xml"]
        pages, seen, today = [], 0, dt.date.today()
        while queue and seen < 8:
            sm = queue.pop(0)
            seen += 1
            try:
                r = httpx.get(sm, timeout=25, follow_redirects=True, verify=False, headers={"User-Agent": config.BOT_UA})
                if r.status_code != 200:
                    continue
                body = r.content
                if body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                found, kids = lenses.parse_sitemap(body[:8_000_000].decode("utf-8", "ignore"))
            except Exception:
                continue
            pages += found
            kids.sort(key=lambda k: 0 if lenses.EVENT_PATH.search(urlparse(k).path) else 1)
            queue += [k for k in kids[:6] if k not in queue]
        good = [(u, m) for u, m in pages if lenses.site_family(u) == fam and lenses.event_like(u, m, today)]
        good.sort(key=lambda x: x[1] or "", reverse=True)
        self._sitemaps[fam] = list(dict.fromkeys(u for u, _ in good))[:limit]
        return self._sitemaps[fam]

    # -------------------------------------------------------- main entry
    def load(self, url: str) -> Snapshot:
        snap = Snapshot(url=url, final_url=url)
        if not self.robots.allowed(url):
            snap.error = "blocked by robots.txt"
            return snap
        if urlparse(url).path.lower().endswith(".pdf"):
            time.sleep(config.DELAY_SECONDS)
            return self._load_pdf(snap)
        if self._dead:
            self._restart()
        watchdog = threading.Timer(config.PAGE_HARD_LIMIT_S, self._freeze)
        watchdog.daemon = True
        page = None
        captured = []

        def on_response(resp):
            try:
                if len(captured) >= 40 or resp.request.resource_type not in ("xhr", "fetch"):
                    return
                if "json" not in (resp.headers.get("content-type") or "").lower():
                    return
                host = urlparse(resp.url).netloc
                if NOT_PROGRAMME_HOSTS.search(host) or lenses.is_ticket_seller(resp.url):
                    return
                captured.append(resp)
            except Exception:
                pass
        try:
            page = self._ctx.new_page()
            page.on("response", on_response)
            watchdog.start()
            resp = None
            for attempt in (1, 2):
                try:
                    # "commit" = the server has answered. Slow sites often take long to finish loading their
                    # scripts; waiting for that made 7 sites time out, so read whatever has arrived instead.
                    resp = page.goto(url, wait_until="commit", timeout=config.PAGE_TIMEOUT_MS * attempt)
                    break
                except Exception as e:
                    if "Download is starting" in str(e):
                        page.close()
                        return self._load_pdf(snap)
                    if attempt == 2:
                        raise
                    page.wait_for_timeout(5000)
            snap.status = resp.status if resp else 0
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PWTimeout:
                pass
            self._wait_out_check(page)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass
            snap.cookie = self._handle_cookies(page)
            self._expand(page)
            text = self._text(page)
            if len(text) < THIN_CHARS:                          # late JavaScript: give it more time once
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(4000)
                if snap.cookie == "none":
                    snap.cookie = self._handle_cookies(page)
                text = self._text(page)
            snap.final_url = page.url
            snap.title = page.title()
            snap.text = text
            try:
                html = page.content()
            except Exception:
                html = ""
            snap.blocked = detect_block(snap.status, snap.title, text, html)
            links = page.eval_on_selector_all(       # SVG links have an object as href; take its text form
                "a[href]", "els => els.map(a => [(a.innerText || a.title || a.textContent || '').trim().slice(0, 140),"
                           " typeof a.href === 'string' ? a.href : (a.getAttribute('href') || '')])")
            seen, clean = set(), []
            for t, h in links:
                if not isinstance(h, str):
                    continue
                h = urljoin(page.url, h).split("#")[0]
                if h.startswith("http") and h not in seen:
                    seen.add(h)
                    clean.append((re.sub(r"\s+", " ", t), h))
            snap.links = clean[:500]
            raw = page.eval_on_selector_all('script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
            for block in raw:
                try:
                    _flatten_ld(json.loads(block), snap.events)
                except Exception:
                    pass
            og = page.locator('meta[property="og:image"]').first
            if og.count():
                snap.og_image = urljoin(snap.final_url, og.get_attribute("content") or "")
            snap.feeds = self._feeds(page, captured)
            snap.events += self._calendar_files([h for _, h in snap.links])
            self._shot(page, snap, problem=bool(snap.blocked) or len(text) < THIN_CHARS
                       or snap.cookie == "banner remains")
        except Exception as e:  # network errors, timeouts, crashes
            snap.error = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            watchdog.cancel()
            if self._dead:
                snap.error = f"page froze for {config.PAGE_HARD_LIMIT_S}s; browser restarted"
            else:
                try:
                    if page is not None and not page.is_closed():
                        page.close()
                except Exception:
                    pass
            time.sleep(config.DELAY_SECONDS)
        return snap
