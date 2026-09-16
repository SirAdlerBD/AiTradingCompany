"""Benchmark tracking from day 0: same start capital, bought into the index ETF once."""
from __future__ import annotations

import sqlite3
from datetime import date

from .config import Config
from .datapack import fetch_quote, mark_price, resolve_instrument
from .mcp_client import McpClient


async def snapshot(cfg: Config, saxo: McpClient, con: sqlite3.Connection, run_id: str | None = None,
                   today: date | None = None) -> dict:
    b = cfg.benchmark
    # includeNonTradable: an EU retail SIM account may not be allowed to trade the
    # ETF, but we only need its price.
    inst = await resolve_instrument(cfg, saxo, con, b.symbol, b.exchange, b.currency,
                                    asset_types="Etf", include_non_tradable=True)
    quote = await fetch_quote(cfg, saxo, inst)
    price = mark_price(quote)
    day = (today or date.today()).isoformat()
    first = con.execute("SELECT units FROM benchmark_snapshots ORDER BY date LIMIT 1").fetchone()
    units = first["units"] if first else b.start_capital / price
    row = {"date": day, "symbol": b.symbol, "currency": inst.get("currency") or b.currency,
           "price": price, "units": units, "value": units * price}
    con.execute(
        "INSERT OR REPLACE INTO benchmark_snapshots(date, symbol, currency, price, units, value, run_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (day, row["symbol"], row["currency"], price, units, row["value"], run_id),
    )
    con.commit()
    return row
