"""FMP wiring: grouped tools with an endpoint argument, `keep` allowlists, and the fundamentals analyst."""
import json
from datetime import date

from mcp.server.mcpserver import MCPServer

from desk import analysts, cli
from desk.db import connect
from tests.conftest import make_saxo

D0 = date(2026, 9, 16)


def make_fmp(calls=None):
    srv = MCPServer("fake-fmp")
    log = calls if calls is not None else []
    tick = {"n": 0}

    @srv.tool()
    def company(endpoint: str, symbol: str | None = None, limit: int | None = None) -> list:
        """Company data"""
        log.append(("company", endpoint, symbol))
        tick["n"] += 1
        if endpoint == "profile-symbol":
            return [{"symbol": symbol, "price": 494.16 + tick["n"], "marketCap": 3669409788000 + tick["n"], "beta": 1.108,
                     "companyName": "Microsoft Corporation", "sector": "Technology", "industry": "Software - Infrastructure",
                     "currency": "USD", "exchange": "NASDAQ", "country": "US", "lastDividend": 3.64, "ipoDate": "1986-03-13",
                     "isActivelyTrading": True, "volume": 1673719 + tick["n"]}]
        return []

    @srv.tool()
    def statements(endpoint: str, symbol: str | None = None, period: str | None = None, limit: int | None = None) -> list:
        """Statements"""
        log.append(("statements", endpoint, symbol, period))
        if endpoint == "key-metrics-ttm":
            return [{"symbol": symbol, "marketCap": 1, "evToSalesTTM": 11.38, "returnOnEquityTTM": 0.332, "earningsYieldTTM": 0.0364}]
        if endpoint == "metrics-ratios-ttm":
            return [{"symbol": symbol, "priceToEarningsRatioTTM": 27.46, "netProfitMarginTTM": 0.403, "debtToEquityRatioTTM": 0.291}]
        if endpoint == "financial-statement-growth":
            return [{"fiscalYear": "2026", "revenueGrowth": 0.178, "netIncomeGrowth": 0.313, "period": period},
                    {"fiscalYear": "2025", "revenueGrowth": 0.149, "netIncomeGrowth": 0.155, "period": period},
                    {"fiscalYear": "2024", "revenueGrowth": 0.10, "netIncomeGrowth": 0.11, "period": period},
                    {"fiscalYear": "2023", "revenueGrowth": 0.05, "netIncomeGrowth": 0.06, "period": period}]
        return []

    @srv.tool()
    def analyst(endpoint: str, symbol: str | None = None) -> list:
        """Analyst"""
        log.append(("analyst", endpoint, symbol))
        return [{"symbol": symbol, "targetHigh": 690, "targetLow": 490, "targetConsensus": 553.39, "targetMedian": 535}]

    return srv


async def test_fundamentals_land_in_pack_reduced_and_stable(cfg):
    cfg.fmp_mcp.enabled = True
    cfg.pipeline.analysts, cfg.pipeline.trader = [], None
    con = connect(cfg.storage.db_path)
    calls = []
    for _ in range(2):
        await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), fmp_inproc=make_fmp(calls), today=D0, log=lambda *_: None)
    rows = con.execute("SELECT stable_json, stable_hash FROM data_packs ORDER BY created_at").fetchall()
    assert rows[0]["stable_hash"] == rows[1]["stable_hash"]          # volatile FMP fields were dropped by `keep`
    fund = json.loads(rows[0]["stable_json"])["fundamentals"]
    assert set(fund) == {"profile", "metrics_ttm", "ratios_ttm", "growth", "price_targets"}
    assert fund["profile"]["sector"] == "Technology" and "price" not in fund["profile"] and "marketCap" not in fund["profile"]
    assert fund["metrics_ttm"]["evToSalesTTM"] == 11.38 and "marketCap" not in fund["metrics_ttm"]
    assert isinstance(fund["growth"], list) and len(fund["growth"]) == 3 and fund["growth"][0]["fiscalYear"] == "2026"
    assert fund["price_targets"] == {"targetLow": 490, "targetConsensus": 553.39, "targetMedian": 535, "targetHigh": 690}
    # every configured fetch went through the grouped tool with its endpoint and the symbol
    assert ("statements", "financial-statement-growth", "MSFT", "annual") in calls
    assert ("company", "profile-symbol", "MSFT") in calls

    # the fundamentals analyst sees fundamentals + a price summary, not the bars
    stable = json.loads(rows[0]["stable_json"])
    system, user, fields = analysts.build_prompt(cfg, "fundamentals_analyst", stable, "2026-09-15")
    assert "fundamentals analyst" in system and "fundamentals.ratios_ttm.priceToEarningsRatioTTM" in fields
    assert "fundamentals.growth[0].revenueGrowth" in fields and "indicators_summary.return_250d" in fields
    assert not any(k.startswith("bars_last_20") for k in fields)
    # and the trader's sector lookup finds it
    assert cli._sector(stable) == "Technology"


def test_prompt_file_override_and_sections(cfg, tmp_path):
    f = tmp_path / "custom.txt"
    f.write_text("You are a custom analyst. Output JSON.")
    cfg.roles["technical_analyst"].prompt_file = str(f)
    cfg.roles["technical_analyst"].sections = ["indicators"]
    stable = {"ticker": {"symbol": "X"}, "instrument": {"description": "X Corp"}, "indicators": {"last_close": 1.0}, "bars": [{"close": 1}]}
    system, user, fields = analysts.build_prompt(cfg, "technical_analyst", stable, "2026-09-15")
    assert system.startswith("You are a custom analyst") and list(fields) == ["indicators.last_close"]
