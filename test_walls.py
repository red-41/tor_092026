"""The browser against local pages that imitate what real venue sites throw at a bot:
bot walls, cookie banners of several kinds, consent walls, overlays, late JavaScript, PDFs."""
import http.server
import io
import threading

import pytest

from collector import config

config.DELAY_SECONDS = 0
config.CHALLENGE_WAIT_S = 4

SHOWS = "".join(f"<p>Swan Lake, {d} October 2026, 19:30, Main Stage. Tickets from 20 EUR.</p>" for d in range(1, 40))
PAGE = "<html><head><title>{title}</title></head><body>{body}</body></html>"


def pdf_bytes():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Ballet season: Giselle, 14 November 2026, 19:30, Grand Theatre")
    c.save()
    return buf.getvalue()


ROUTES = {
    "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private\n"),
    "/private": (200, "text/html", PAGE.format(title="x", body=SHOWS)),
    # Cloudflare wall that never clears
    "/cf": (403, "text/html", PAGE.format(title="Just a moment...", body=
            "<h1>example.org</h1><p>Checking your browser before accessing example.org.</p>"
            "<p>Enable JavaScript and cookies to continue</p><script>window._cf_chl_opt={cvId:'3'}</script>")),
    # check page that lets a real browser through after 1.5 s
    "/cf-pass": (200, "text/html", PAGE.format(title="Just a moment...", body=
                 "<p>Checking your browser</p><script>setTimeout(()=>{document.title='Season';"
                 "document.body.innerHTML='<h1>Season</h1>" + SHOWS.replace("'", "") + "'},1500)</script>")),
    # Akamai-style refusal
    "/denied": (403, "text/html", PAGE.format(title="Access Denied", body=
                "<h1>Access Denied</h1><p>You don't have permission to access this server.</p>"
                "<p>Reference #18.2f3e1402.1695.abc123</p>")),
    # normal page that happens to have a reCAPTCHA newsletter form
    "/normal": (200, "text/html", PAGE.format(title="Ballet", body=
                "<h1>Ballet</h1>" + SHOWS + "<form><div class='g-recaptcha'></div>This site is protected by reCAPTCHA</form>")),
    # OneTrust banner
    "/onetrust": (200, "text/html", PAGE.format(title="Dance", body=SHOWS +
                  "<div id='onetrust-banner-sdk' style='position:fixed;bottom:0;left:0;right:0;height:200px;background:#fff'>"
                  "We use cookies to improve your experience."
                  "<button id='onetrust-accept-btn-handler'>Accept All</button>"
                  "<button id='onetrust-reject-all-handler' onclick=\"document.getElementById('onetrust-banner-sdk').remove()\">"
                  "Reject All</button></div>")),
    # banner inside an iframe (Sourcepoint style), Danish button text
    "/iframe": (200, "text/html", PAGE.format(title="Dans", body=SHOWS +
                "<iframe id='sp_message_iframe_1' style='position:fixed;bottom:0;left:0;width:100%;height:220px' "
                "srcdoc=\"<p>Vi bruger cookies</p><button onclick=&quot;parent.document.getElementById('sp_message_iframe_1').remove()&quot;>"
                "Afvis alle</button><button>Accepter alle</button>\"></iframe>")),
    # consent wall: nothing to read until you accept
    "/wall": (200, "text/html", PAGE.format(title="Programme", body=
              "<div id='main'></div><div id='cw' style='position:fixed;inset:0;background:#fff'>"
              "<p>We need your consent to use cookies before showing this page.</p>"
              "<button onclick=\"document.getElementById('cw').remove();document.getElementById('main').innerHTML="
              "'<h1>Giselle</h1>" + SHOWS.replace("'", "") + "'\">Accept all</button><button>Settings</button></div>")),
    # banner with no reject/accept wording that covers the Load more button
    "/overlay": (200, "text/html", PAGE.format(title="Calendar", body=
                 SHOWS + "<div id='list'></div><button id='more' onclick=\"document.getElementById('list').innerHTML="
                 "'<p>Extra event: Bolero, 30 December 2026, 20:00</p>';this.remove()\">Load more</button>"
                 "<div class='consent-layer' style='position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99'>"
                 "<div style='background:#fff;margin:200px auto;width:500px;height:150px'>This site uses cookies."
                 "<button>Preferences</button></div></div>")),
    # content arrives 4 s after load
    "/late": (200, "text/html", PAGE.format(title="Loading", body=
              "<div id='app'>Loading...</div><script>setTimeout(()=>{document.getElementById('app').innerHTML="
              "'<h1>Season</h1>" + SHOWS.replace("'", "") + "'},4000)</script>")),
    "/empty": (200, "text/html", PAGE.format(title="Coming soon", body="<p>New season coming soon.</p>")),
    # SVG links (their href is an object, which crashed the Iceland Dance Company page)
    "/svg": (200, "text/html", PAGE.format(title="Svg", body=SHOWS +
             "<svg width='200' height='50'><a href='/programme'><text x='5' y='20'>Programme</text></a></svg>"
             "<a href='/season'>Season</a>")),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":                     # answers at once, finishes loading 5 s later
            head = ("<html><head><title>Slow</title></head><body><h1>Season</h1>" + SHOWS).encode()
            tail = b"<p>Carmen, 3 March 2027, 19:00</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(head) + len(tail)))
            self.end_headers()
            self.wfile.write(head)
            self.wfile.flush()
            import time
            time.sleep(5)
            self.wfile.write(tail)
            return
        if self.path == "/schedule.pdf":
            status, ctype, body = 200, "application/pdf", pdf_bytes()
        else:
            status, ctype, body = ROUTES.get(self.path, (404, "text/html", "<h1>Not found</h1>"))
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def br(tmp_path_factory):
    from collector.browse import Browser
    with Browser(shots_dir=str(tmp_path_factory.mktemp("shots")), shots="problems") as b:
        yield b


def test_cloudflare_wall_is_reported_as_blocked(site, br):
    s = br.load(site + "/cf")
    assert s.blocked == "cloudflare" and s.status == 403 and s.screenshot


def test_check_page_that_clears_is_read(site, br):
    s = br.load(site + "/cf-pass")
    assert not s.blocked and "Swan Lake" in s.text and s.title == "Season"


def test_akamai_refusal(site, br):
    assert br.load(site + "/denied").blocked == "akamai"


def test_recaptcha_form_is_not_a_wall(site, br):
    s = br.load(site + "/normal")
    assert not s.blocked and "Swan Lake" in s.text and s.cookie == "none"


def test_onetrust_refused(site, br):
    s = br.load(site + "/onetrust")
    assert s.cookie == "refused" and "We use cookies" not in s.text


def test_banner_in_iframe_refused_in_danish(site, br):
    assert br.load(site + "/iframe").cookie == "refused"


def test_consent_wall_accepted_only_when_nothing_to_read(site, br):
    s = br.load(site + "/wall")
    assert s.cookie == "accepted" and "Giselle" in s.text


def test_overlay_removed_and_load_more_clicked(site, br):
    s = br.load(site + "/overlay")
    assert s.cookie == "hidden" and "Bolero" in s.text


def test_late_javascript_is_waited_for(site, br):
    s = br.load(site + "/late")
    assert "Swan Lake" in s.text and not s.blocked


def test_empty_page_is_not_called_blocked(site, br):
    s = br.load(site + "/empty")
    assert not s.blocked and not s.error and len(s.text) < 100 and s.screenshot


def test_pdf_schedule(site, br):
    s = br.load(site + "/schedule.pdf")
    assert "Giselle" in s.text and not s.error


def test_robots_txt_respected(site, br):
    assert br.load(site + "/private").error == "blocked by robots.txt"


def test_preflight_verdicts_and_summary(site, br):
    from collector.preflight import summarize, verdict
    v = {p: verdict(br.load(site + p)) for p in ("/cf", "/empty", "/normal", "/private", "/nope")}
    assert v == {"/cf": "BLOCKED", "/empty": "EMPTY", "/normal": "OK", "/private": "ROBOTS", "/nope": "ERROR"}
    rows = [{"source": p, "priority": 1, "url": site + p, "verdict": k, "blocked_by": "", "error": "",
             "text_chars": 10, "cookie_banner": "none", "screenshot": ""} for p, k in v.items()]
    md = summarize(rows, "Preflight")
    assert "| BLOCKED | 1 |" in md and "Needs a look" in md


def test_slow_site_is_read_instead_of_timing_out(site, br):
    old = config.PAGE_TIMEOUT_MS
    config.PAGE_TIMEOUT_MS = 3000            # shorter than the site takes to finish
    try:
        s = br.load(site + "/slow")
    finally:
        config.PAGE_TIMEOUT_MS = old
    assert not s.error and "Carmen" in s.text


def test_svg_links_do_not_crash(site, br):
    s = br.load(site + "/svg")
    assert not s.error, s.error
    urls = [h for _, h in s.links]
    assert site + "/programme" in urls and site + "/season" in urls
