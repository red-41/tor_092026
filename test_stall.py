"""The safeguards added after the run of 27 October, when 6 of 12 jobs froze on one website each."""
import http.server
import subprocess
import sys
import textwrap
import threading
import time

import httpx
import pytest

import browse


class Trickle(http.server.BaseHTTPRequestHandler):
    """A server that sends one byte every half second for ever: no single read ever times out."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        try:
            for _ in range(10_000):
                self.wfile.write(b"<")
                self.wfile.flush()
                time.sleep(0.5)
        except Exception:
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def trickle():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/sitemap.xml"
    srv.shutdown()


def test_a_slow_trickling_download_is_cut_off(trickle):
    t = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        browse.fetch(trickle, timeout=5, total_s=2)
    assert time.monotonic() - t < 6


def test_a_pdf_that_never_finishes_is_stopped(monkeypatch):
    monkeypatch.setattr(browse, "PDF_SCRIPT", "import time\nwhile True: time.sleep(1)")
    t = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        browse.pdf_text(b"%PDF-1.4", seconds=2)
    assert time.monotonic() - t < 10


def test_a_job_without_progress_prints_where_it_is_stuck_and_ends(tmp_path):
    script = tmp_path / "stuck.py"
    script.write_text(textwrap.dedent("""
        import time, config, stall
        config.STALL_MINUTES = 2 / 60          # two seconds
        stall.arm()
        for _ in range(3):                     # progress keeps it alive
            time.sleep(1)
            stall.progress(force=True)
        def stuck_on_a_website():
            time.sleep(60)
        stuck_on_a_website()
    """))
    t = time.monotonic()
    p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                       cwd=str(browse.__file__).rsplit("/", 1)[0], env={"PYTHONPATH": str(browse.__file__).rsplit("/", 1)[0]})
    assert p.returncode != 0 and time.monotonic() - t < 20
    assert "Timeout" in p.stderr and "stuck_on_a_website" in p.stderr


# ---------- the browser itself (a real Chromium, a local test page) ----------

class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><head><title>Ballet</title></head><body>" + b"<p>Giselle 12 March 2027 19:30</p>" * 40 + b"</body></html>"
        self.send_response(200 if self.path != "/robots.txt" else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _hang(br, page):
    page.evaluate("new Promise(() => {})")          # a browser call that never gets an answer


@pytest.mark.parametrize("level", [1, 2])
def test_a_browser_call_that_never_answers_is_broken_and_the_next_page_works(site, monkeypatch, level):
    import config
    monkeypatch.setattr(config, "PAGE_HARD_LIMIT_S", 3)
    monkeypatch.setattr(config, "WATCH_DRIVER_S", 3)
    monkeypatch.setattr(config, "DELAY_SECONDS", 0)
    if level == 2:                                   # stopping the browser does not help: the driver must go too
        real = browse._kill_children
        monkeypatch.setattr(browse, "_kill_children", lambda driver: real(driver) if driver else 0)
    with browse.Browser() as br:
        monkeypatch.setattr(browse.Browser, "_expand", _hang)
        t = time.monotonic()
        stuck = br.load(site + "/a")
        assert "froze" in (stuck.error or "") and time.monotonic() - t < 30
        monkeypatch.setattr(browse.Browser, "_expand", lambda self, page: None)
        monkeypatch.setattr(config, "PAGE_HARD_LIMIT_S", 120)       # a normal page needs more than 3 s
        if level == 2:
            monkeypatch.setattr(browse, "_kill_children", real)
        ok = br.load(site + "/b")
        assert not ok.error and "Giselle" in ok.text
