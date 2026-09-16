"""Build the per-ticker data pack: the only thing analysts ever see.

Shapes below follow saxo-mcp (src/tools/marketdata.ts):
  search_instruments  -> {count, hint, instruments: [{Identifier, Symbol, Description,
                          AssetType, ExchangeId, CurrencyCode, ...}]}
  get_chart_data      -> {count, chartInfo, displayAndFormat, bars: [{Time, Open, High,
                          Low, Close, Volume?, Interest?}]}
  get_instrument_price-> Saxo infoprice: {Quote: {Bid, Ask, Mid, MarketState?, DelayedByMinutes?},
                          PriceInfo: {High, Low, NetChange, PercentChange},
                          PriceInfoDetails: {LastTraded, ...}, LastUpdated, ...}
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from typing import Any

from .config import Config
from .db import j, now
from .mcp_client import McpClient


class InstrumentNotFound(LookupError):
    pass


def _sym(s: Any) -> str:
    """Saxo symbols look like 'MSFT:xnas'; compare the part before the colon."""
    return str(s or "").split(":")[0].upper()


def match_instrument(hits: list[dict[str, Any]], symbol: str, exchange: str | None,
                     currency: str | None) -> dict[str, Any]:
    """Pick the one search result that matches symbol, exchange and currency.
    Falls back in that order; raises if the symbol never matches."""
    by_symbol = [h for h in hits if _sym(h.get("Symbol")) == symbol.upper()]
    if not by_symbol:
        raise InstrumentNotFound(f"no search hit with Symbol {symbol!r} among {[h.get('Symbol') for h in hits]}")
    cands = by_symbol
    if exchange:
        m = [h for h in cands if str(h.get("ExchangeId", "")).upper() == exchange.upper()]
        cands = m or cands
    if currency:
        m = [h for h in cands if str(h.get("CurrencyCode", "")).upper() == currency.upper()]
        cands = m or cands
    return cands[0]


async def resolve_instrument(cfg: Config, saxo: McpClient, con: sqlite3.Connection | None,
                             symbol: str, exchange: str, currency: str | None,
                             asset_types: str = "Stock",
                             include_non_tradable: bool = False) -> dict[str, Any]:
    if con is not None:
        row = con.execute("SELECT uic, asset_type, currency, description, saxo_symbol FROM instruments "
                          "WHERE symbol=? AND exchange=?", (symbol, exchange)).fetchone()
        if row:
            return {"uic": row["uic"], "asset_type": row["asset_type"], "currency": row["currency"],
                    "description": row["description"], "saxo_symbol": row["saxo_symbol"]}

    args: dict[str, Any] = {"keywords": symbol, "assetTypes": asset_types, "exchangeId": exchange, "top": 50}
    if include_non_tradable:
        args["includeNonTradable"] = True
    res = await saxo.call(cfg.saxo_mcp.tools["search"], args)
    hits = res.get("instruments", []) if isinstance(res, dict) else (res or [])
    hit = match_instrument(hits, symbol, exchange, currency)
    inst = {
        "uic": int(hit["Identifier"]),
        "asset_type": str(hit.get("AssetType") or asset_types.split(",")[0]),
        "currency": hit.get("CurrencyCode"),
        "description": hit.get("Description"),
        "saxo_symbol": hit.get("Symbol"),
    }
    if con is not None:
        con.execute(
            "INSERT OR REPLACE INTO instruments(symbol, exchange, uic, asset_type, currency, description, saxo_symbol, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, exchange, inst["uic"], inst["asset_type"], inst["currency"], inst["description"],
             inst["saxo_symbol"], now()),
        )
        con.commit()
    return inst


async def fetch_bars(cfg: Config, saxo: McpClient, inst: dict[str, Any], count: int) -> list[dict[str, Any]]:
    raw = await saxo.call(cfg.saxo_mcp.tools["chart"], {
        "uic": inst["uic"], "assetType": inst["asset_type"], "horizon": 1440, "count": count,
    })
    return normalise_bars(raw)


async def fetch_quote(cfg: Config, saxo: McpClient, inst: dict[str, Any]) -> dict[str, Any]:
    raw = await saxo.call(cfg.saxo_mcp.tools["price"], {"uic": inst["uic"], "assetType": inst["asset_type"]})
    return normalise_quote(raw)


async def build(cfg: Config, saxo: McpClient, fmp: McpClient | None, con: sqlite3.Connection | None, t,
                today: date | None = None) -> dict[str, Any]:
    inst = await resolve_instrument(cfg, saxo, con, t.symbol, t.exchange, t.currency, "Stock")
    bars = await fetch_bars(cfg, saxo, inst, cfg.universe.history_days)
    bars = drop_partial(bars, today or date.today())
    quote = await fetch_quote(cfg, saxo, inst)

    stable: dict[str, Any] = {
        "ticker": t.model_dump(),
        "instrument": inst,
        "bars": bars,
        "fundamentals": {},
    }
    if fmp is not None:
        for key, tool in cfg.fmp_mcp.tools.items():
            if tool:
                stable["fundamentals"][key] = await fmp.call(tool, {cfg.fmp_mcp.symbol_arg: t.symbol})

    return {
        "stable": stable,
        "stable_hash": hashlib.sha256(j(stable).encode()).hexdigest(),
        "volatile": {"quote": quote},
    }


def normalise_bars(raw: Any) -> list[dict[str, Any]]:
    """Reduce saxo-mcp's chart payload to [{date, open, high, low, close, volume}], sorted by date."""
    rows = raw.get("bars", raw.get("Data")) if isinstance(raw, dict) else raw
    out = []
    for r in rows or []:
        d = str(r.get("Time") or r.get("time") or r.get("date") or "")[:10]
        if not d:
            continue
        out.append({
            "date": d,
            "open": _f(r.get("Open", r.get("open"))), "high": _f(r.get("High", r.get("high"))),
            "low": _f(r.get("Low", r.get("low"))), "close": _f(r.get("Close", r.get("close"))),
            "volume": _f(r.get("Volume", r.get("volume"))),
        })
    return sorted(out, key=lambda x: x["date"])


def drop_partial(bars: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Drop today's bar. It is partial while the market is open and, even after
    close, keeping it would make a 12:00 run and a 23:00 run hash differently."""
    t = today.isoformat()
    return [b for b in bars if b["date"] < t]


def normalise_quote(raw: Any) -> dict[str, Any]:
    q = raw.get("Quote", {}) if isinstance(raw, dict) else {}
    pi = raw.get("PriceInfo", {}) if isinstance(raw, dict) else {}
    pid = raw.get("PriceInfoDetails", {}) if isinstance(raw, dict) else {}
    bid, ask = _f(q.get("Bid")), _f(q.get("Ask"))
    mid = _f(q.get("Mid"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2
    last = _f(pid.get("LastTraded"))
    if mid is None:
        mid = last
    return {
        "bid": bid, "ask": ask, "mid": mid, "last": last,
        "day_high": _f(pi.get("High")), "day_low": _f(pi.get("Low")),
        "net_change": _f(pi.get("NetChange")), "pct_change": _f(pi.get("PercentChange")),
        "market_state": q.get("MarketState"),
        "delayed_by_minutes": q.get("DelayedByMinutes"),
        "updated": raw.get("LastUpdated") if isinstance(raw, dict) else None,
    }


def mark_price(quote: dict[str, Any]) -> float:
    """The price used for marking: mid, else last. Raises rather than guessing."""
    for k in ("mid", "last"):
        if quote.get(k) is not None:
            return float(quote[k])
    raise ValueError(f"quote carries no usable price: {quote}")


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
