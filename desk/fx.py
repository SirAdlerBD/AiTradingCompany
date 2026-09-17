"""FX conversion for the shadow ledger.

The account currency is `cfg.benchmark.currency`. Every fill and every mark of
a position in a different currency must be converted before it can be summed
against cash or the benchmark, the same way a bank statement never adds
dollars to euros. The rate used is frozen once per run, the same way a price
mark is frozen, so a value computed today is reproducible from stored data
tomorrow: nothing here ever calls out for a "live" rate at read time.

Convention: `rate[CCY]` converts 1 unit of CCY into account currency:
    value_in_account_ccy = native_value * rate[CCY]
The account currency itself is never fetched and is implicitly 1.0.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from .config import Config
from .datapack import fetch_quote, mark_price
from .db import now
from .mcp_client import McpClient


class FxUnavailable(RuntimeError):
    pass


async def resolve_pair(cfg: Config, saxo: McpClient, con: sqlite3.Connection | None,
                       account: str, foreign: str) -> tuple[dict[str, Any], str]:
    """Find the FxSpot instrument for account<->foreign and how to read its price.

    Saxo has one instrument per pair (e.g. "EURUSD" for EUR/USD), not two, and its
    keyword search matches the pair regardless of which order the two currency
    codes are typed in. So this never trusts the query order: it inspects the
    *returned* Symbol to tell which of the two directions the quote is in.
      - Symbol == account+foreign (e.g. EURUSD when account=EUR): price is
        "foreign units per 1 account unit" -> rate = 1 / price ("F_PER_A").
      - Symbol == foreign+account: price is "account units per 1 foreign unit"
        -> rate = price directly ("A_PER_F").
    Resolved instruments are cached in the `instruments` table under
    exchange='fx' (a MIC never has that value) so repeat lookups are free.
    """
    if con is not None:
        for sym in (f"{account}{foreign}", f"{foreign}{account}"):
            row = con.execute(
                "SELECT uic, asset_type, saxo_symbol FROM instruments WHERE symbol=? AND exchange='fx'", (sym,)
            ).fetchone()
            if row:
                inst = {"uic": row["uic"], "asset_type": row["asset_type"]}
                direction = "F_PER_A" if row["saxo_symbol"].upper() == f"{account}{foreign}" else "A_PER_F"
                return inst, direction

    res = await saxo.call(cfg.saxo_mcp.tools["search"], {"keywords": f"{account}{foreign}", "assetTypes": "FxSpot", "top": 10})
    hits = res.get("instruments", []) if isinstance(res, dict) else (res or [])
    want = {account.upper(), foreign.upper()}
    hit = next((h for h in hits if isinstance(h.get("Symbol"), str) and len(h["Symbol"]) == 6
               and {h["Symbol"][:3].upper(), h["Symbol"][3:].upper()} == want), None)
    if hit is None:
        raise FxUnavailable(f"no FxSpot instrument for {account}/{foreign} among {[h.get('Symbol') for h in hits]}")
    sym = hit["Symbol"].upper()
    inst = {"uic": int(hit["Identifier"]), "asset_type": str(hit.get("AssetType") or "FxSpot")}
    direction = "F_PER_A" if sym == f"{account}{foreign}" else "A_PER_F"
    if con is not None:
        con.execute(
            "INSERT OR REPLACE INTO instruments(symbol, exchange, uic, asset_type, currency, description, saxo_symbol, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sym, "fx", inst["uic"], inst["asset_type"], hit.get("CurrencyCode"), hit.get("Description"), sym, now()),
        )
        con.commit()
    return inst, direction


async def rate_to_account(cfg: Config, saxo: McpClient, con: sqlite3.Connection | None,
                          account: str, foreign: str) -> float:
    if account == foreign:
        return 1.0
    try:
        inst, direction = await resolve_pair(cfg, saxo, con, account, foreign)
        price = mark_price(await fetch_quote(cfg, saxo, inst))
    except FxUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - any lookup/quote failure is an FX-unavailable day, not a crash
        raise FxUnavailable(f"{foreign}/{account}: {e}") from e
    if price <= 0:
        raise FxUnavailable(f"{foreign}/{account}: non-positive quote {price}")
    return (1.0 / price) if direction == "F_PER_A" else price


async def snapshot(cfg: Config, saxo: McpClient, con: sqlite3.Connection, run_id: str,
                   today: date) -> tuple[dict[str, float], list[str]]:
    """Fetch and store today's rate for every currency the universe actually uses that
    differs from the account currency. Returns (rates fetched, error strings for the
    rest) so the caller can warn per currency without failing the run: a missing rate
    just leaves affected fills pending, exactly like a missing price does."""
    account = cfg.benchmark.currency
    needed = sorted({t.currency for t in cfg.universe.tickers if t.currency != account})
    rates: dict[str, float] = {}
    errors: list[str] = []
    for ccy in needed:
        try:
            rate = await rate_to_account(cfg, saxo, con, account, ccy)
        except FxUnavailable as e:
            errors.append(f"{ccy}->{account}: {e}")
            continue
        con.execute("INSERT OR REPLACE INTO fx_rates(date, currency, rate, run_id) VALUES (?,?,?,?)",
                    (today.isoformat(), ccy, rate, run_id))
        con.commit()
        rates[ccy] = rate
    return rates, errors
