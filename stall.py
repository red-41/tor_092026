"""Stall guard. A job that makes no progress for STALL_MINUTES prints where every thread is stuck (in the job's log,
starting with "Timeout") and ends, instead of hanging until GitHub stops it after almost six hours.

Run of 27 October: 6 of the 12 jobs froze, each on one website (Kunstenfestivaldesarts, Teatro Circo Murcia,
Holstebro, La Ménagerie de Verre, Ballet du Nord, Teatro alla Scala), and lost every source they had not saved yet.

Python's faulthandler does the waiting in its own C thread, so it works even when the stuck code holds the
interpreter (a long regular expression, a PDF library in a loop) and no Python thread could run."""
import faulthandler
import sys
import time

import config

_armed = False
_last = 0.0


def arm() -> None:
    """Start guarding this job (run.py collect)."""
    global _armed
    _armed = True
    progress(force=True)


def progress(force: bool = False) -> None:
    """The job moved on (a page opened, a batch polled, a source started): restart the countdown."""
    global _last
    if not _armed:
        return
    now = time.monotonic()
    if not force and now - _last < 5:
        return
    _last = now
    faulthandler.dump_traceback_later(config.STALL_MINUTES * 60, exit=True, file=sys.stderr)


def disarm() -> None:
    global _armed
    _armed = False
    faulthandler.cancel_dump_traceback_later()
