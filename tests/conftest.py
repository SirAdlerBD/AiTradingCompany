"""A fake saxo-mcp, in process, with the exact tool names, argument names and
payload shapes of saxo-mcp/src/tools/*.ts. No network anywhere in the tests."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from desk import config as cfgmod

SIM_KEY = "SimAcc123"
# Shapes and ExchangeId codes as observed on the real SIM server.
INSTRUMENTS = {
    "MSFT": {"Identifier": 1234, "Symbol": "MSFT:xnas", "Description": "Microsoft Corp.",
             "AssetType": "Stock", "ExchangeId": "NASDAQ", "CurrencyCode": "USD"},
    "SXR8": {"Identifier": 9876, "Symbol": "SXR8:xetr", "Description": "iShares Core S&P 500 UCITS",
             "AssetType": "Etf", "ExchangeId": "XETR_ETF", "CurrencyCode": "EUR"},
}


def make_saxo(*, trading="DISABLED (hard block): this server cannot place, modify or cancel orders.",
              account_keys=(SIM_KEY,), today: date | None = None, quote_mid=100.0, calls=None):
    """Returns an MCPServer. `calls` (a list) records every tool invocation."""
    today = today or date.today()
    srv = MCPServer("fake-saxo")
    log = calls if calls is not None else []

    @srv.tool()
    def get_account_summary() -> dict:
        """Account summary"""
        log.append(("get_account_summary", {}))
        return {
            "environment": "Values come from the environment configured in SAXO_ENV (sim during development).",
            "trading": trading,
            "user": {"UserId": "u1", "ClientKey": "Ck"},
            "client": {"ClientKey": "Ck", "DefaultAccountKey": account_keys[0]},
            "accounts": [{"AccountKey": k, "Currency": "EUR", "AccountType": "Normal"} for k in account_keys],
        }

    @srv.tool()
    def search_instruments(keywords: str, assetTypes: str | None = None, exchangeId: str | None = None,
                           top: int | None = None, includeNonTradable: bool | None = None) -> dict:
        """Search instruments"""
        log.append(("search_instruments", {"keywords": keywords, "assetTypes": assetTypes,
                                           "exchangeId": exchangeId, "includeNonTradable": includeNonTradable}))
        # Real Saxo behaviour: a wrong ExchangeId filter silently returns nothing.
        if exchangeId and exchangeId.upper() not in {i["ExchangeId"] for i in INSTRUMENTS.values()}:
            return {"count": 0, "hint": "Use Identifier as `uic`...", "instruments": []}
        # decoys first, like a real search: same ticker on another venue, and a near-miss ticker
        hits = [{"Identifier": 5555, "Symbol": f"{keywords.upper()}:xmil", "Description": "decoy",
                 "AssetType": "Stock", "ExchangeId": "MIL", "CurrencyCode": "EUR"},
                {"Identifier": 5556, "Symbol": f"1{keywords.upper()}:xnas", "Description": "decoy",
                 "AssetType": "Stock", "ExchangeId": "NASDAQ", "CurrencyCode": "USD"}]
        hits += [i for i in INSTRUMENTS.values() if keywords.upper() in i["Symbol"].upper()]
        return {"count": len(hits), "hint": "Use Identifier as `uic`...", "instruments": hits}

    @srv.tool()
    def get_chart_data(uic: int, assetType: str, horizon: int, count: int | None = None,
                       time: str | None = None, mode: str | None = None) -> dict:
        """Historical OHLC bars"""
        log.append(("get_chart_data", {"uic": uic, "assetType": assetType, "horizon": horizon, "count": count}))
        n = count or 1200
        bars = []
        for i in range(n - 1, -1, -1):
            d = today - timedelta(days=i)
            px = 100 + (uic % 7) + ((n - i) % 5)
            bars.append({"Time": f"{d.isoformat()}T00:00:00.000000Z", "Open": px, "High": px + 1,
                         "Low": px - 1, "Close": px + 0.5, "Volume": 1000 + i, "Interest": 0})
        return {"count": len(bars), "chartInfo": {"Horizon": horizon}, "displayAndFormat": {}, "bars": bars}

    @srv.tool()
    def get_instrument_price(uic: int, assetType: str) -> dict:
        """Current price quote"""
        log.append(("get_instrument_price", {"uic": uic, "assetType": assetType}))
        # Volatile on purpose: every call returns a different quote.
        srv_state["ticks"] = srv_state.get("ticks", 0) + 1
        mid = quote_mid + srv_state["ticks"] * 0.01
        return {
            "AssetType": assetType, "Uic": uic, "LastUpdated": f"{today.isoformat()}T15:00:{srv_state['ticks']:02d}Z",
            "Quote": {"Bid": mid - 0.05, "Ask": mid + 0.05, "Mid": mid, "MarketState": "Open", "DelayedByMinutes": 15},
            "PriceInfo": {"High": mid + 1, "Low": mid - 1, "NetChange": 0.3, "PercentChange": 0.3},
            "PriceInfoDetails": {"LastTraded": mid - 0.01},
        }

    srv_state: dict = {}
    return srv


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> cfgmod.Config:
    monkeypatch.setenv("SAXO_SIM_ACCOUNT_KEYS", SIM_KEY)
    monkeypatch.setenv("SAXO_SIM_MCP_TOKEN", "t")
    monkeypatch.delenv("SAXO_ALLOW_LIVE", raising=False)
    c = cfgmod.load(Path(__file__).parent.parent / "config" / "desk.yaml")
    c.storage.db_path = tmp_path / "desk.sqlite"
    c.universe.history_days = 30
    return c
