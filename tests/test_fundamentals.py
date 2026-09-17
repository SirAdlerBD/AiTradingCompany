"""FMP wiring: grouped tools with an endpoint argument, `keep` allowlists, and the fundamentals analyst."""
import json
from datetime import date

import httpx

from desk import analysts, cli
from desk.fmp import FmpClient
from desk.db import connect
from tests.conftest import make_saxo

D0 = date(2026, 9, 16)


ROWS = {
    "profile": lambda n: [{"symbol": "MSFT", "price": 494.16 + n, "marketCap": 3669409788000 + n, "beta": 1.108,
                           "companyName": "Microsoft Corporation", "sector": "Technology", "industry": "Software - Infrastructure",
                           "currency": "USD", "exchange": "NASDAQ", "country": "US", "lastDividend": 3.64, "ipoDate": "1986-03-13",
                           "isActivelyTrading": True, "volume": 1673719 + n}],
    "key-metrics-ttm": lambda n: [{"symbol": "MSFT", "marketCap": 1, "evToSalesTTM": 11.38, "returnOnEquityTTM": 0.332, "earningsYieldTTM": 0.0364}],
    "ratios-ttm": lambda n: [{"symbol": "MSFT", "priceToEarningsRatioTTM": 27.46, "netProfitMarginTTM": 0.403, "debtToEquityRatioTTM": 0.291}],
    "financial-growth": lambda n: [{"fiscalYear": "2026", "revenueGrowth": 0.178, "netIncomeGrowth": 0.313},
                                   {"fiscalYear": "2025", "revenueGrowth": 0.149, "netIncomeGrowth": 0.155},
                                   {"fiscalYear": "2024", "revenueGrowth": 0.10, "netIncomeGrowth": 0.11},
                                   {"fiscalYear": "2023", "revenueGrowth": 0.05, "netIncomeGrowth": 0.06}],
    "price-target-consensus": lambda n: [{"symbol": "MSFT", "targetHigh": 690, "targetLow": 490, "targetConsensus": 553.39, "targetMedian": 535}],
}


class FakeRest:
    """httpx.get stand-in: records calls, answers like FMP's stable API, volatile fields change per call."""

    def __init__(self, fail: dict[str, int] | None = None):
        self.calls = []
        self.n = 0
        self.fail = fail or {}

    def __call__(self, url, params=None, timeout=None):
        self.n += 1
        path = url.rsplit("/", 1)[-1]
        self.calls.append((path, dict(params or {})))
        req = httpx.Request("GET", url)
        if path in self.fail:
            return httpx.Response(self.fail[path], text="upstream trouble", request=req)
        if path not in ROWS:
            return httpx.Response(404, text="not found", request=req)
        return httpx.Response(200, json=ROWS[path](self.n), request=req)


def make_fmp(cfg, monkeypatch, **kw):
    monkeypatch.setenv("FMP_API_KEY", "k")
    fake = FakeRest(**kw)
    return FmpClient(cfg.fmp_rest, get=fake), fake


async def test_fundamentals_land_in_pack_reduced_and_stable(cfg, monkeypatch):
    cfg.fmp_rest.enabled = True
    cfg.pipeline.analysts, cfg.pipeline.trader = [], None
    con = connect(cfg.storage.db_path)
    client, fake = make_fmp(cfg, monkeypatch)
    for _ in range(2):
        await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), fmp_client=client, today=D0, log=lambda *_: None)
    calls = fake.calls
    rows = con.execute("SELECT stable_json, stable_hash FROM data_packs ORDER BY created_at").fetchall()
    assert rows[0]["stable_hash"] == rows[1]["stable_hash"]          # volatile FMP fields were dropped by `keep`
    fund = json.loads(rows[0]["stable_json"])["fundamentals"]
    assert set(fund) == {"profile", "metrics_ttm", "ratios_ttm", "growth", "price_targets"}
    assert fund["profile"]["sector"] == "Technology" and "price" not in fund["profile"] and "marketCap" not in fund["profile"]
    assert fund["metrics_ttm"]["evToSalesTTM"] == 11.38 and "marketCap" not in fund["metrics_ttm"]
    assert isinstance(fund["growth"], list) and len(fund["growth"]) == 3 and fund["growth"][0]["fiscalYear"] == "2026"
    assert fund["price_targets"] == {"targetLow": 490, "targetConsensus": 553.39, "targetMedian": 535, "targetHigh": 690}
    # every configured fetch hit its path with the symbol, its params and the key as a query parameter
    growth = next(p for path, p in calls if path == "financial-growth")
    assert growth == {"period": "annual", "limit": 3, "symbol": "MSFT", "apikey": "k"}
    assert ("profile", {"symbol": "MSFT", "apikey": "k"}) in calls

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


async def test_fmp_failure_is_a_warning_not_a_failed_run(cfg, monkeypatch):
    cfg.fmp_rest.enabled = True
    cfg.fmp_rest.retries = 1
    cfg.fmp_rest.backoff_s = 0
    cfg.pipeline.analysts, cfg.pipeline.trader = [], None
    con = connect(cfg.storage.db_path)
    client, fake = make_fmp(cfg, monkeypatch, fail={"key-metrics-ttm": 503})
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), fmp_client=client, today=D0, log=lambda *_: None)
    r = con.execute("SELECT status, warnings FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert r["status"] == "ok" and "section unavailable, metrics_ttm" in r["warnings"] and "2 attempt" in r["warnings"]
    assert sum(1 for p, _ in fake.calls if p == "key-metrics-ttm") == 2           # retried once
    stable = json.loads(con.execute("SELECT stable_json FROM data_packs WHERE run_id=?", (run_id,)).fetchone()["stable_json"])
    assert stable["fundamentals"]["_unavailable"] == ["metrics_ttm"] and "profile" in stable["fundamentals"]

    # missing key: fmp disabled for the run with a warning, run still ok
    monkeypatch.delenv("FMP_API_KEY")
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), today=D0, log=lambda *_: None)
    r = con.execute("SELECT status, warnings FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert r["status"] == "ok" and "FMP_API_KEY is not set" in r["warnings"]


def test_fmp_check_reports_missing_keep_fields_and_bad_paths(cfg, monkeypatch, capsys):
    client, fake = make_fmp(cfg, monkeypatch)
    cfg.fmp_rest.fetch["growth"].path = "financial-statement-growth"        # wrong path -> 404
    cfg.fmp_rest.fetch["profile"].keep.append("marketCapX")                  # field FMP does not return
    cfg.fmp_rest.fetch["metrics_ttm"].keep = ["evToSalesTTM"]                # the fake returns only a few fields
    cfg.fmp_rest.fetch["ratios_ttm"].keep = ["priceToEarningsRatioTTM"]
    assert cli.cmd_fmp_check(cfg, "MSFT", client=client) == 1
    out = capsys.readouterr().out
    assert "FAIL growth" in out and "HTTP 404" in out
    assert "OK   profile" in out and "MISSING keep fields: ['marketCapX']" in out
    assert "OK   ratios_ttm" in out and "2 problem(s)" in out


async def test_partial_fundamentals_on_402_keep_the_rest_and_tell_the_analyst(cfg, monkeypatch):
    cfg.fmp_rest.enabled = True
    cfg.fmp_rest.retries = 0
    cfg.pipeline.analysts, cfg.pipeline.trader = [], None
    con = connect(cfg.storage.db_path)
    client, fake = make_fmp(cfg, monkeypatch, fail={"key-metrics-ttm": 402, "ratios-ttm": 402})
    for _ in range(2):
        run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), fmp_client=client, today=D0, log=lambda *_: None)
    r = con.execute("SELECT status, warnings FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert r["status"] == "ok"
    assert "MSFT: fundamentals section unavailable, metrics_ttm: subscription tier (HTTP 402" in r["warnings"]
    assert "ratios_ttm: subscription tier" in r["warnings"]
    rows = con.execute("SELECT stable_json, stable_hash FROM data_packs ORDER BY created_at").fetchall()
    assert rows[0]["stable_hash"] == rows[1]["stable_hash"]                    # names only in the pack, not error text
    fund = json.loads(rows[-1]["stable_json"])["fundamentals"]
    assert set(fund) == {"profile", "growth", "price_targets", "_unavailable"}
    assert fund["_unavailable"] == ["metrics_ttm", "ratios_ttm"] and fund["profile"]["sector"] == "Technology"
    # the analyst sees the surviving sections, no marker field, and a plain note about the gap
    stable = json.loads(rows[-1]["stable_json"])
    system, user, fields = analysts.build_prompt(cfg, "fundamentals_analyst", stable, "2026-09-15")
    assert "fundamentals.growth[0].revenueGrowth" in fields and not any("_unavailable" in k for k in fields)
    assert "UNAVAILABLE fundamentals sections for this instrument (outside the data subscription): metrics_ttm, ratios_ttm" in user
    assert cli._sector(stable) == "Technology"                                 # sector rule still works


async def test_all_sections_failing_is_still_an_empty_pack_with_a_warning(cfg, monkeypatch):
    cfg.fmp_rest.enabled = True
    cfg.fmp_rest.retries = 0
    cfg.pipeline.analysts, cfg.pipeline.trader = [], None
    con = connect(cfg.storage.db_path)
    client, _ = make_fmp(cfg, monkeypatch, fail={p: 402 for p in ("profile", "key-metrics-ttm", "ratios-ttm", "financial-growth", "price-target-consensus")})
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), fmp_client=client, today=D0, log=lambda *_: None)
    r = con.execute("SELECT status, warnings FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert r["status"] == "ok" and "fmp fetch failed, fundamentals empty" in r["warnings"]
    stable = json.loads(con.execute("SELECT stable_json FROM data_packs WHERE run_id=?", (run_id,)).fetchone()["stable_json"])
    assert stable["fundamentals"] == {}
    _, user, _ = analysts.build_prompt(cfg, "fundamentals_analyst", stable, "2026-09-15")
    assert "UNAVAILABLE" not in user


def test_fmp_check_names_the_subscription_tier(cfg, monkeypatch, capsys):
    cfg.fmp_rest.retries = 0
    client, _ = make_fmp(cfg, monkeypatch, fail={"key-metrics-ttm": 402})
    cfg.fmp_rest.fetch["ratios_ttm"].keep = ["priceToEarningsRatioTTM"]
    assert cli.cmd_fmp_check(cfg, "XIOR", client=client) == 1
    out = capsys.readouterr().out
    assert "FAIL metrics_ttm" in out and "subscription tier (HTTP 402" in out and "not a config problem" in out
