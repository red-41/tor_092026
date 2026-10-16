"""Spending cap: every Claude request is paid from a small block reserved in the database first.

The ledger (collector_spend) is shared by the 12 parallel collect jobs and the duplicate check of one workflow run
(they share GITHUB_RUN_ID). A block is refused when it would take the calendar month (UTC) past
MONTHLY_BUDGET_USD or the run past RUN_BUDGET_USD; the collector then stops sending pages, exactly as when the
Anthropic credit runs out (what was found is kept, the rest stays due).

Costs are estimated before a request is sent (generously) and corrected when its answer comes back, so the ledger
ends close to what Anthropic bills. Prices: config.PRICES (USD per million tokens).
"""
import os
import time

import config
import db


def price(model: str) -> tuple[float, float, float, float]:
    """(input, output, cache write 5m, cache read) per million tokens; an unknown model costs like the dearest."""
    return config.PRICES.get(model) or max(config.PRICES.values())


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0,
             batch: bool = False) -> float:
    p_in, p_out, p_cw, p_cr = price(model)
    prompt = (input_tokens or 0) + (cache_write or 0) + (cache_read or 0)
    factor = config.LONG_PROMPT_FACTOR.get(model, 1.0) if prompt > config.LONG_PROMPT_TOKENS else 1.0
    usd = ((input_tokens or 0) * p_in + (output_tokens or 0) * p_out + (cache_write or 0) * p_cw
           + (cache_read or 0) * p_cr) / 1_000_000 * factor
    return usd * (0.5 if batch else 1.0)


def usage_cost(model: str, usage, batch: bool = False) -> float:
    """Actual cost of one answer from its usage block."""
    g = (lambda k: getattr(usage, k, None) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k) or 0)
    return cost_usd(model, g("input_tokens"), g("output_tokens"), g("cache_creation_input_tokens"),
                    g("cache_read_input_tokens"), batch)


def estimate_tokens(text: str) -> int:
    """Generous token estimate: about 3 characters a token for Latin text, fewer for Cyrillic, Greek and the like."""
    if not text:
        return 0
    other = sum(1 for ch in text if ord(ch) > 0x24F)
    return int((len(text) - other) / 3.0 + other / 1.6) + 1


def estimate_request(params: dict, batch: bool = False) -> float:
    """Cost estimate of one request before it is sent: the fixed instructions read from the prompt cache, the page
    text at the full input price, and an answer of EST_OUTPUT_TOKENS."""
    model = params.get("model") or config.MODEL
    fixed = sum(estimate_tokens(b.get("text", "")) for b in params.get("system") or [] if isinstance(b, dict))
    fixed += estimate_tokens(str(params.get("tools") or ""))
    user = "".join(m["content"] if isinstance(m.get("content"), str) else str(m.get("content"))
                   for m in params.get("messages") or [])
    return cost_usd(model, estimate_tokens(user), config.EST_OUTPUT_TOKENS, cache_read=fixed, batch=batch)


class BudgetReached(Exception):
    """The monthly or per-run spending cap would be passed."""


class Budget:
    """Pays requests from blocks reserved in the shared ledger."""

    def __init__(self, part: str = "collect", shard: int = 0, run_id: str | None = None,
                 month_cap: float | None = None, run_cap: float | None = None, block: float | None = None):
        self.part, self.shard = part, shard
        self.run_id = run_id or os.environ.get("GITHUB_RUN_ID") or f"local-{int(time.time())}"
        self.month_cap = config.MONTHLY_BUDGET_USD if month_cap is None else month_cap
        self.run_cap = config.RUN_BUDGET_USD if run_cap is None else run_cap
        self.block = config.BUDGET_BLOCK_USD if block is None else block
        self.available = 0.0            # reserved and not yet used
        self.used_estimate = 0.0        # estimates of requests sent
        self.actual = 0.0               # actual cost of answers received
        self.answered_estimate = 0.0    # estimates of those answers
        self._flushed = 0.0             # corrections already written
        self._last_flush = time.time()
        self.stopped: str | None = None
        self.last: dict = {}

    def _reserve(self, want: float) -> None:
        try:
            r = db.rpc("spend_reserve", {"p_run": self.run_id, "p_shard": self.shard, "p_part": self.part,
                                         "p_usd": round(want, 6), "p_month_cap": self.month_cap,
                                         "p_run_cap": self.run_cap}, timeout=60)
        except Exception as e:
            # without the ledger nothing is spent: safer to stop than to run blind
            self.stopped = f"spending ledger unavailable ({str(e)[:120]})"
            raise BudgetReached(self.stopped)
        self.last = r or {}
        if not (r or {}).get("ok"):
            which = "monthly" if (r or {}).get("why") == "month" else "per-run"
            cap = (r or {}).get("month_cap") if which == "monthly" else (r or {}).get("run_cap")
            self.stopped = (f"the {which} spending cap of ${cap} is reached "
                            f"(this month ${float((r or {}).get('month') or 0):.2f}, "
                            f"this run ${float((r or {}).get('run') or 0):.2f})")
            raise BudgetReached(self.stopped)
        self.available += want

    def open(self) -> bool:
        """Reserve the first block at the start of a job: False when the cap is already reached."""
        if self.stopped:
            return False
        if self.available <= 0.000001:
            try:
                self._reserve(self.block)
            except BudgetReached:
                return False
        return True

    def spend(self, usd: float) -> None:
        """Pay an estimated cost, reserving a new block when needed; raises BudgetReached at the cap."""
        if self.stopped:
            raise BudgetReached(self.stopped)
        if usd > self.available:
            self._reserve(max(self.block, usd - self.available))
        self.available -= usd
        self.used_estimate += usd

    def answered(self, estimate: float, actual: float) -> None:
        """An answer came back: remember its actual cost (written to the ledger now and then)."""
        self.answered_estimate += estimate
        self.actual += actual
        if time.time() - self._last_flush > 120:
            self.flush()

    def flush(self, note: str | None = None) -> None:
        delta = (self.actual - self.answered_estimate) - self._flushed
        self._last_flush = time.time()
        if abs(delta) < 0.000001:
            return
        try:
            db.rpc("spend_record", {"p_run": self.run_id, "p_shard": self.shard, "p_part": self.part,
                                    "p_kind": "adjust", "p_usd": round(delta, 6), "p_note": note}, timeout=60)
            self._flushed += delta
        except Exception as e:
            print(f"spending ledger: could not record a correction ({str(e)[:120]})", flush=True)

    def close(self) -> None:
        """End of the job: write the last corrections and give back what was reserved and not used."""
        self.flush()
        if self.available > 0.000001:
            try:
                db.rpc("spend_record", {"p_run": self.run_id, "p_shard": self.shard, "p_part": self.part,
                                        "p_kind": "release", "p_usd": round(-self.available, 6), "p_note": None},
                       timeout=60)
                self.available = 0.0
            except Exception as e:
                print(f"spending ledger: could not release the unused reservation ({str(e)[:120]})", flush=True)

    def summary(self) -> str:
        spent = self.used_estimate - self.answered_estimate + self.actual
        line = f"about ${spent:.2f}"
        month = self.last.get("month")
        if month is not None:
            line += f" (this month so far about ${float(month):.2f} of ${self.month_cap:.0f})"
        return line


class NoBudget:
    """No cap (tests, or a run without the database)."""
    stopped = None
    actual = used_estimate = answered_estimate = 0.0

    def open(self) -> bool:
        return True

    def spend(self, usd: float) -> None:
        self.used_estimate += usd

    def answered(self, estimate: float, actual: float) -> None:
        self.answered_estimate += estimate
        self.actual += actual

    def flush(self, note=None) -> None:
        pass

    def close(self) -> None:
        pass

    def summary(self) -> str:
        return f"about ${self.used_estimate - self.answered_estimate + self.actual:.2f}"
