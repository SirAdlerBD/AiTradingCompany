"""Shadow ledger: code-simulated fills at the next quote the system sees.

A decision made after today's close becomes a pending decision; the next run
fills it at that run's mark price. No look-ahead, and the fill is visible in
`fills` with its fee. The book (positions, cash, weights) is derived from
fills so it can always be recomputed from the tables.

Every instrument may be quoted in its own currency; cash and `start_capital`
are always in the account currency (`cfg.benchmark.currency`). A fill's
`value`/`fee` are converted into account currency AT FILL TIME using that
day's frozen rate from `desk/fx.py`, and baked into the stored numbers - so
`cash()` and `positions()` can keep summing `fills.value`/`fills.fee` with no
further conversion, exactly as before this module knew about currencies.
Only a *live* mark (today's quote, still in native currency) needs converting
at read time; that happens in `book()`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .config import Config
from .db import j, now
from .risk import BookContext, RuleSet, dd_halted

def ticker_currency(cfg: Config, con: sqlite3.Connection, ticker: str) -> str:
    """The instrument's own currency: the resolved Saxo instrument if it has been
    looked up (authoritative), else the ticker's declared currency in config,
    else the account currency (so an unresolvable ticker never crashes a read)."""
    row = con.execute(
        "SELECT currency FROM instruments WHERE symbol=? AND currency IS NOT NULL AND currency!='' LIMIT 1", (ticker,)
    ).fetchone()
    if row and row["currency"]:
        return row["currency"]
    t = next((t for t in cfg.universe.tickers if t.symbol == ticker), None)
    if t:
        return t.currency
    return cfg.benchmark.currency


def known_rates(con: sqlite3.Connection, as_of: str) -> dict[str, float]:
    """The latest fx_rates row per currency on or before `as_of` (YYYY-MM-DD). Carries
    forward like a price mark: a quiet FX day still has its last known rate."""
    out: dict[str, float] = {}
    for r in con.execute(
        "SELECT currency, rate FROM fx_rates WHERE date <= ? AND (currency, date) IN "
        "(SELECT currency, MAX(date) FROM fx_rates WHERE date <= ? GROUP BY currency)", (as_of, as_of)
    ):
        out[r["currency"]] = float(r["rate"])
    return out


def fx_for(cfg: Config, con: sqlite3.Connection, ticker: str, as_of: str,
          rates: dict[str, float] | None = None) -> tuple[str, float | None]:
    """(currency, rate-to-account) for a ticker on a given day. rate is None, never a
    guess, when the currency differs from the account currency and no rate is known."""
    ccy = ticker_currency(cfg, con, ticker)
    if ccy == cfg.benchmark.currency:
        return ccy, 1.0
    rates = known_rates(con, as_of) if rates is None else rates
    return ccy, rates.get(ccy)


@dataclass
class Position:
    ticker: str
    quantity: float
    cost_basis: float          # total cost incl. fees of the open lot
    opened_at: str             # date of the first fill of the current open lot


def positions(con: sqlite3.Connection) -> dict[str, Position]:
    """Rebuild open positions from fills, oldest first."""
    pos: dict[str, Position] = {}
    for f in con.execute("SELECT ticker, quantity, price, COALESCE(fee,0) AS fee, filled_at FROM fills ORDER BY id"):
        t = f["ticker"]
        p = pos.get(t)
        q = float(f["quantity"])
        if p is None or p.quantity <= 1e-12:
            if q > 0:
                pos[t] = Position(t, q, q * f["price"] + f["fee"], f["filled_at"][:10])
            continue
        if q > 0:
            p.quantity += q
            p.cost_basis += q * f["price"] + f["fee"]
        else:
            frac = min(1.0, -q / p.quantity) if p.quantity else 1.0
            p.cost_basis *= (1 - frac)
            p.quantity += q
            if p.quantity <= 1e-9:
                del pos[t]
    return pos


def cash(cfg: Config, con: sqlite3.Connection) -> float:
    row = con.execute("SELECT COALESCE(SUM(value),0) AS v, COALESCE(SUM(fee),0) AS f FROM fills").fetchone()
    return cfg.benchmark.start_capital - float(row["v"]) - float(row["f"])


def mark_prices(con: sqlite3.Connection, run_id: str, mark: str) -> dict[str, float]:
    """Today's price per ticker from this run's data packs (quote mid or last)."""
    out = {}
    for r in con.execute("SELECT ticker, volatile_json FROM data_packs WHERE run_id=?", (run_id,)):
        q = json.loads(r["volatile_json"] or "{}").get("quote", {})
        px = q.get(mark) if q.get(mark) is not None else q.get("mid") if q.get("mid") is not None else q.get("last")
        if px is not None:
            out[r["ticker"]] = float(px)
    return out


def book(cfg: Config, con: sqlite3.Connection, prices: dict[str, float], today: date | None = None) -> dict[str, Any]:
    """`prices` are native-currency marks (from `mark_prices`); `cost_basis` (from fills)
    is already in account currency. `today` selects which day's fx rate applies to the
    live mark; a ticker whose currency has no known rate is valued at its last known
    account-currency value instead of guessing, and listed under `_fx_missing`."""
    today = today or date.today()
    pos = positions(con)
    c = cash(cfg, con)
    rates = known_rates(con, today.isoformat())
    rows = {}
    total = c
    fx_missing: list[str] = []
    for t, p in pos.items():
        px = prices.get(t)
        ccy, fx = fx_for(cfg, con, t, today.isoformat(), rates)
        if px is None or fx is None:  # no quote today, or no fx rate today: carry at last known value
            value = _last_value(con, t)
            if value is None:
                value = p.cost_basis  # never been marked before: fall back to cost
            if fx is None and ccy != cfg.benchmark.currency:
                fx_missing.append(t)
        else:
            value = p.quantity * px * fx
        total += value
        rows[t] = {"quantity": round(p.quantity, 6), "price": px, "currency": ccy, "fx_rate": fx,
                   "value": round(value, 2), "cost_basis": round(p.cost_basis, 2),
                   "unrealized_pct": round(value / p.cost_basis - 1, 4) if p.cost_basis else 0.0,
                   "opened_at": p.opened_at}
    for t in rows:
        rows[t]["weight"] = round(rows[t]["value"] / total, 6) if total else 0.0
    out = {"cash": round(c, 2), "total_value": round(total, 2), "positions": rows}
    if fx_missing:
        out["_fx_missing"] = sorted(fx_missing)
    return out


def _last_price(con: sqlite3.Connection, ticker: str) -> float | None:
    r = con.execute("SELECT positions_json FROM portfolio_snapshots ORDER BY date DESC LIMIT 1").fetchone()
    if not r:
        return None
    return (json.loads(r["positions_json"]).get(ticker) or {}).get("price")


def _last_value(con: sqlite3.Connection, ticker: str) -> float | None:
    """Last known account-currency value of a position, from the most recent snapshot
    that held it. Used when neither today's price nor today's fx rate is available,
    so a quiet day never silently mixes currencies."""
    for r in con.execute("SELECT positions_json FROM portfolio_snapshots ORDER BY date DESC"):
        v = (json.loads(r["positions_json"]).get(ticker) or {}).get("value")
        if v is not None:
            return float(v)
    return None


def fill_pending(cfg: Config, con: sqlite3.Connection, run_id: str, prices: dict[str, float], today: date,
                 log=print) -> list[dict[str, Any]]:
    """Fill every pending decision at today's mark. `prices` are native currency; a fill
    is sized and valued in the instrument's own currency, then converted to account
    currency at today's rate before it is stored - `value`/`fee` are always account
    currency from here on, so cash() and positions() never need to know about FX. A
    ticker with no price today, or no fx rate today, stays pending: never guess a rate."""
    filled = []
    rates = known_rates(con, today.isoformat())
    pending = con.execute("SELECT * FROM decisions WHERE status='pending' ORDER BY id").fetchall()
    for d in pending:
        t = d["ticker"]
        px = prices.get(t)
        if px is None:
            log(f"{t}: decision {d['id']} still pending, no price today")
            continue
        ccy, fx = fx_for(cfg, con, t, today.isoformat(), rates)
        if fx is None:
            log(f"{t}: decision {d['id']} still pending, no {ccy}->{cfg.benchmark.currency} fx rate today")
            continue
        b = book(cfg, con, prices, today)
        cur_qty = b["positions"].get(t, {}).get("quantity", 0.0)
        cur_val = cur_qty * px * fx
        target_val = float(d["final_weight"]) * b["total_value"]
        delta_val = target_val - cur_val
        if d["action"] == "exit":
            delta_val = -cur_val
        qty = delta_val / (px * fx)
        if not cfg.ledger.allow_fractional:
            qty = float(int(qty)) if qty > 0 else -float(int(-qty))
        value = qty * px * fx        # account currency, signed: +buy, -sell
        if abs(value) < 1.0:          # below one account-currency unit: nothing to do
            con.execute("UPDATE decisions SET status='noop', filled_at=? WHERE id=?", (now(), d["id"]))
            con.commit()
            continue
        if qty < 0:
            qty = max(qty, -cur_qty)
            value = qty * px * fx
        fee = abs(value) * cfg.ledger.fee_bps / 1e4
        con.execute(
            "INSERT INTO fills(decision_id, source, ticker, quantity, price, filled_at, fee, value, run_id, currency, fx_rate) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], "shadow", t, qty, px, f"{today.isoformat()}T{now()[11:19]}Z", fee, value, run_id, ccy, fx),
        )
        con.execute("UPDATE decisions SET status='filled', filled_at=? WHERE id=?", (now(), d["id"]))
        con.commit()
        filled.append({"ticker": t, "quantity": qty, "price": px, "currency": ccy, "fx_rate": fx, "fee": fee, "decision_id": d["id"]})
        fx_note = "" if fx == 1.0 else f" (fx {ccy}->{cfg.benchmark.currency} {fx:.5f})"
        log(f"{t}: filled decision {d['id']} {d['action']} qty {qty:.4f} @ {px:.2f} {ccy} fee {fee:.2f}{fx_note}")
    return filled


def snapshot(cfg: Config, con: sqlite3.Connection, run_id: str, prices: dict[str, float], today: date) -> dict[str, Any]:
    b = book(cfg, con, prices, today)
    prev = con.execute("SELECT peak_value FROM portfolio_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
                       (today.isoformat(),)).fetchone()
    peak = max(float(prev["peak_value"]) if prev and prev["peak_value"] else cfg.benchmark.start_capital, b["total_value"])
    dd = b["total_value"] / peak - 1 if peak else 0.0
    con.execute(
        "INSERT OR REPLACE INTO portfolio_snapshots(date, cash, positions_json, total_value, run_id, peak_value, drawdown) "
        "VALUES (?,?,?,?,?,?,?)",
        (today.isoformat(), b["cash"], j(b["positions"]), b["total_value"], run_id, peak, round(dd, 6)),
    )
    con.commit()
    b["drawdown"] = round(dd, 6)
    return b


def context(cfg: Config, con: sqlite3.Connection, rules: RuleSet, prices: dict[str, float], today: date,
            sectors: dict[str, str | None]) -> BookContext:
    b = book(cfg, con, prices, today)
    weights = {t: r["weight"] for t, r in b["positions"].items()}
    values = [float(r["total_value"]) for r in con.execute("SELECT total_value FROM portfolio_snapshots ORDER BY date")]
    values.append(b["total_value"])
    r = rules.get("PORTFOLIO_DD_HALT")
    halted, dd = (dd_halted(values, r.limit, float(r.params.get("resume_at", r.limit))) if r and r.limit is not None
                  else (False, 0.0))
    r = rules.get("MAX_WEEKLY_TURNOVER")
    window = int(r.params.get("window_days", 7)) if r else 7
    since = (datetime.fromisoformat(today.isoformat()) ).date().toordinal() - window
    traded = con.execute("SELECT COALESCE(SUM(ABS(value)),0) FROM fills WHERE substr(filled_at,1,10) > ?",
                         (date.fromordinal(since).isoformat(),)).fetchone()[0]
    turnover = float(traded) / b["total_value"] if b["total_value"] else 0.0
    r = rules.get("MAX_CORR_TO_BOOK")
    lookback = int(r.params.get("lookback_days", 60)) if r else 60
    returns = daily_returns(con, lookback)
    days_held = {t: (today - date.fromisoformat(p["opened_at"])).days for t, p in b["positions"].items()}
    return BookContext(total_value=b["total_value"], cash=b["cash"], weights=weights, sectors=sectors,
                       drawdown=dd, dd_halted=halted, turnover_window=turnover, returns=returns, days_held=days_held)


def daily_returns(con: sqlite3.Connection, lookback: int) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for t in [r[0] for r in con.execute("SELECT DISTINCT ticker FROM price_history")]:
        closes = [r[0] for r in con.execute(
            "SELECT close FROM price_history WHERE ticker=? AND close IS NOT NULL ORDER BY date DESC LIMIT ?",
            (t, lookback + 1))][::-1]
        if len(closes) > 1:
            out[t] = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
    return out


def active_stop(con: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    """The stop attached to the latest filled long/hold decision for the ticker."""
    r = con.execute(
        "SELECT p.stop_json FROM decisions d JOIN trader_proposals p ON p.id=d.proposal_id "
        "WHERE d.ticker=? AND d.action IN ('long','hold') AND d.status='filled' ORDER BY d.id DESC LIMIT 1", (ticker,)
    ).fetchone()
    return json.loads(r["stop_json"]) if r and r["stop_json"] else None
