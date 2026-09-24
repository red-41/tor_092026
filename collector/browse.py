"""Headless browser: open a page, refuse cookies, expand lists, and return its text, links and event data."""
import json
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import config

REJECT_WORDS = re.compile(
    r"(reject all|reject|refuse|decline|deny|only necessary|necessary only|essential only|"
    r"alle ablehnen|ablehnen|nur notwendige|tout refuser|refuser|continuer sans accepter|"
    r"weigeren|alles weigeren|rifiuta|rifiuta tutto|rechazar|rechazar todo|avvisa|afvis|"
    r"kieltäydy|odmítnout|odrzuć|elutasít|recusar|nej tack)",
    re.I,
)
REJECT_SELECTORS = [
    "#onetrust-reject-all-handler", "#CybotCookiebotDialogBodyButtonDecline",
    "[data-testid='uc-deny-all-button']", "#didomi-notice-disagree-button", ".cc-deny",
    "button.cookie-reject", "#tarteaucitronAllDenied2", ".cmplz-deny",
]
LOAD_MORE = re.compile(r"(load more|show more|more events|mehr anzeigen|weitere termine|mehr laden|"
                       r"voir plus|afficher plus|meer laden|toon meer|mostra altri|ver más|pokaż więcej|näytä lisää)", re.I)

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


class Robots:
    def __init__(self):
        self._cache = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = httpx.get(root + "/robots.txt", timeout=15, follow_redirects=True,
                              headers={"User-Agent": config.USER_AGENT})
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                rp.parse([])
            self._cache[root] = rp
        return self._cache[root].can_fetch(config.USER_AGENT, url)


class Browser:
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(user_agent=config.USER_AGENT, locale="en-GB",
                                              viewport={"width": 1366, "height": 900})
        self.robots = Robots()
        return self

    def __exit__(self, *exc):
        self._ctx.close()
        self._browser.close()
        self._pw.stop()

    def _dismiss_cookies(self, page):
        for sel in REJECT_SELECTORS:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=300):
                    el.click(timeout=1500)
                    return
            except Exception:
                pass
        try:
            btn = page.get_by_role("button", name=REJECT_WORDS).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=1500)
        except Exception:
            pass

    def _expand(self, page):
        for _ in range(5):
            try:
                btn = page.get_by_role("button", name=LOAD_MORE).first
                if not btn.is_visible(timeout=500):
                    break
                btn.click(timeout=2000)
                page.wait_for_timeout(1500)
            except Exception:
                break
        for _ in range(4):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(400)

    def load(self, url: str) -> Snapshot:
        snap = Snapshot(url=url, final_url=url)
        if not self.robots.allowed(url):
            snap.error = "blocked by robots.txt"
            return snap
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass
            self._dismiss_cookies(page)
            self._expand(page)
            snap.final_url = page.url
            snap.title = page.title()
            text = page.inner_text("body")
            snap.text = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(a => [(a.innerText || a.title || '').trim().slice(0, 140), a.href])")
            seen, clean = set(), []
            for t, h in links:
                h = h.split("#")[0]
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
        except Exception as e:  # network errors, timeouts, crashes
            snap.error = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            page.close()
            time.sleep(config.DELAY_SECONDS)
        return snap
