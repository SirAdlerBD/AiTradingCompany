"""Shadow ledger: code-simulated fills at the next quote the system sees.

A decision made after today's close becomes a pending decision; the next run
fills it at that run's mark price. No look-ahead, and the fill is visible in
`fills` with its fee. The book (positions, cash, weights) is derived from
fills so it can always be recomputed from the tables.
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


def book(cfg: Config, con: sqlite3.Connection, prices: dict[str, float]) -> dict[str, Any]:
    pos = positions(con)
    c = cash(cfg, con)
    rows = {}
    total = c
    for t, p in pos.items():
        px = prices.get(t)
        if px is None:  # no quote today: carry at last known snapshot price
            px = _last_price(con, t) or p.cost_basis / p.quantity
        value = p.quantity * px
        total += value
        rows[t] = {"quantity": round(p.quantity, 6), "price": px, "value": round(value, 2),
                   "cost_basis": round(p.cost_basis, 2), "unrealized_pct": round(value / p.cost_basis - 1, 4) if p.cost_basis else 0.0,
                   "opened_at": p.opened_at}
    for t in rows:
        rows[t]["weight"] = round(rows[t]["value"] / total, 6) if total else 0.0
    return {"cash": round(c, 2), "total_value": round(total, 2), "positions": rows}


def _last_price(con: sqlite3.Connection, ticker: str) -> float | None:
    r = con.execute("SELECT positions_json FROM portfolio_snapshots ORDER BY date DESC LIMIT 1").fetchone()
    if not r:
        return None
    return (json.loads(r["positions_json"]).get(ticker) or {}).get("price")


def fill_pending(cfg: Config, con: sqlite3.Connection, run_id: str, prices: dict[str, float], today: date,
                 log=print) -> list[dict[str, Any]]:
    """Fill every pending decision at today's mark. A ticker with no price today stays pending."""
    filled = []
    pending = con.execute("SELECT * FROM decisions WHERE status='pending' ORDER BY id").fetchall()
    for d in pending:
        t = d["ticker"]
        px = prices.get(t)
        if px is None:
            log(f"{t}: decision {d['id']} still pending, no price today")
            continue
        b = book(cfg, con, prices)
        cur_qty = b["positions"].get(t, {}).get("quantity", 0.0)
        cur_val = cur_qty * px
        target_val = float(d["final_weight"]) * b["total_value"]
        delta_val = target_val - cur_val
        if d["action"] == "exit":
            delta_val = -cur_val
        qty = delta_val / px
        if not cfg.ledger.allow_fractional:
            qty = float(int(qty)) if qty > 0 else -float(int(-qty))
        if abs(qty * px) < 1.0:          # below one currency unit: nothing to do
            con.execute("UPDATE decisions SET status='noop', filled_at=? WHERE id=?", (now(), d["id"]))
            con.commit()
            continue
        if qty < 0:
            qty = max(qty, -cur_qty)
        fee = abs(qty * px) * cfg.ledger.fee_bps / 1e4
        con.execute(
            "INSERT INTO fills(decision_id, source, ticker, quantity, price, filled_at, fee, value, run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d["id"], "shadow", t, qty, px, f"{today.isoformat()}T{now()[11:19]}Z", fee, qty * px, run_id),
        )
        con.execute("UPDATE decisions SET status='filled', filled_at=? WHERE id=?", (now(), d["id"]))
        con.commit()
        filled.append({"ticker": t, "quantity": qty, "price": px, "fee": fee, "decision_id": d["id"]})
        log(f"{t}: filled decision {d['id']} {d['action']} qty {qty:.4f} @ {px:.2f} fee {fee:.2f}")
    return filled


def snapshot(cfg: Config, con: sqlite3.Connection, run_id: str, prices: dict[str, float], today: date) -> dict[str, Any]:
    b = book(cfg, con, prices)
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
    b = book(cfg, con, prices)
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
