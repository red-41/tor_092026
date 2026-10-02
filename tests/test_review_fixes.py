"""Review findings of 1 Oct: late answers after the credit runs out, one pending row per page, calendar addresses
spelled differently by the site and by the month generator."""
import datetime as dt
from types import SimpleNamespace

import config
import db
import run
from extract import Batch
from tests.test_rolling import world, Sites, src, PAGES, show, titles  # noqa: F401


def test_credit_out_listing_late(world):
    claude, writes, mp = world
    mp.setattr(run, "Browser", Sites())
    mp.setattr(config, "LISTING_BATCH_MINUTES", 0)
    mp.setattr(db, "due_sources", lambda *a, **k: [src("C", "https://c.org/")])
    calls = {"n": 0}
    ex_holder = {}
    def retrieve(bid):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(processing_status="in_progress")
        run_ex = ex_holder.get("ex")
        return SimpleNamespace(processing_status="ended")
    claude.batches.retrieve = retrieve
    orig = run.apply_batch
    def ab(batch, finished, ex):
        if finished:
            ex.out_of_credit = True     # credit runs out at the moment the late schedule page comes back
        return orig(batch, finished, ex)
    mp.setattr(run, "apply_batch", ab)
    run.main([])
    print(writes["updated"])
    print(titles(writes, "C"))
    assert writes["updated"].get("C", {}).get("status") != "failed"


def test_pending_rows_dupes(world):
    claude, writes, mp = world
    # same final url for two months -> two pending rows same (url, role)
    rows = []
    mp.setattr(db, "pending_put", lambda r: rows.extend(r))
    b = Batch.__new__(Batch)
    snap = SimpleNamespace(final_url="https://x/cal")
    res = {"pending": 2, "_state": {}}
    b._open = {"b1": ["p1", "p2"]}
    b._unsent, b._failed = [], []
    b.context = {"p1": ("more", res, snap, "https://x/cal?m=1", "h"), "p2": ("more", res, snap, "https://x/cal?m=2", "h")}
    run.left_open([b], SimpleNamespace(model="m"))
    print(rows)
    assert len({(r["url"], r["role"]) for r in rows}) == len(rows)



def test_month_encoding_mismatch(world):
    claude, writes, mp = world
    today = dt.date.today()
    y, m = (today.year + (today.month == 12), today.month % 12 + 1)
    months = []
    for _ in range(13):
        months.append((y, m)); y, m = (y + (m == 12), m % 12 + 1)
    def u(ym): return f"https://k.org/cal?genre=dance,ballet&month={ym[0]}-{ym[1]:02d}"
    PAGES["https://k.org/"] = {**show("Nutcracker"), "next_page_url": u(months[0])}
    for i, ym in enumerate(months[:-1]):
        site_next = u(months[i + 1])
        for variant in (u(ym), u(ym).replace(",", "%2C")):
            PAGES[variant] = {**show("Giselle %s" % i), "next_page_url": site_next}
    sites = Sites()
    mp.setattr(run, "Browser", sites)
    mp.setattr(db, "due_sources", lambda *a, **k: [{**src("K", "https://k.org/"), "priority": 0}])
    run.main([])
    opened = [x for x in sites.opened if "k.org" in x]
    import lenses
    keys = [lenses.same_address_key(x) for x in opened]
    assert len(keys) == len(set(keys))                            # no month opened twice under two spellings
    assert len(opened) <= 14                                      # homepage, 12 months, at most the site's own "next"
    for k in [k for k in PAGES if "k.org" in k]:
        PAGES.pop(k)

