"""FX conversion: the resolver's direction logic, and that money in different
currencies is never summed unconverted anywhere in the ledger or performance report."""
import json
from datetime import date

import pytest

from desk import cli, fx, ledger, performance
from desk.config import Ticker
from desk.db import connect
from desk.mcp_client import McpClient
from tests.conftest import make_saxo, seed_fx
from tests.test_analyst import good_view
from tests.test_trader_flow import make_router, proposal, warmup

D0 = date(2026, 9, 16)
D1 = date(2026, 9, 17)


# ---------- fx.py: resolver and rate direction ----------

async def test_resolve_pair_detects_direction_from_the_returned_symbol(cfg):
    saxo = make_saxo(today=D0, fx_mid=1.10)
    async with McpClient(cfg.saxo_mcp, "saxo", inproc=saxo) as c:
        inst, direction = await fx.resolve_pair(cfg, c, None, "EUR", "USD")
        assert direction == "F_PER_A"                       # Symbol EURUSD: base=account(EUR)
        rate = await fx.rate_to_account(cfg, c, None, "EUR", "USD")
        assert rate == pytest.approx(1 / 1.10)               # 1 USD = 1/1.10 EUR

        inst2, direction2 = await fx.resolve_pair(cfg, c, None, "USD", "EUR")
        assert direction2 == "A_PER_F"                       # same instrument, account and foreign swapped
        rate2 = await fx.rate_to_account(cfg, c, None, "USD", "EUR")
        assert rate2 == pytest.approx(1.10)                  # 1 EUR = 1.10 USD


async def test_rate_to_account_is_1_for_the_account_currency(cfg):
    async with McpClient(cfg.saxo_mcp, "saxo", inproc=make_saxo(today=D0)) as c:
        assert await fx.rate_to_account(cfg, c, None, "EUR", "EUR") == 1.0


async def test_resolve_pair_caches_in_the_instruments_table(cfg):
    con = connect(cfg.storage.db_path)
    calls = []
    saxo = make_saxo(today=D0, fx_mid=1.10, calls=calls)
    async with McpClient(cfg.saxo_mcp, "saxo", inproc=saxo) as c:
        r1 = await fx.rate_to_account(cfg, c, con, "EUR", "USD")
        r2 = await fx.rate_to_account(cfg, c, con, "EUR", "USD")
    assert r1 == r2 == pytest.approx(1 / 1.10)
    assert sum(1 for name, _ in calls if name == "search_instruments") == 1     # second call hit the cache
    row = con.execute("SELECT uic, exchange, currency FROM instruments WHERE symbol='EURUSD'").fetchone()
    assert row["exchange"] == "fx" and row["uic"] == 21


async def test_fx_unavailable_when_no_pair_resolves(cfg):
    async with McpClient(cfg.saxo_mcp, "saxo", inproc=make_saxo(today=D0, fx_broken=True)) as c:
        with pytest.raises(fx.FxUnavailable):
            await fx.rate_to_account(cfg, c, None, "EUR", "USD")


# ---------- ledger.py: currency and rate lookup ----------

def test_ticker_currency_prefers_resolved_instrument_over_config(cfg):
    con = connect(cfg.storage.db_path)
    assert ledger.ticker_currency(cfg, con, "MSFT") == "USD"           # config fallback: declared currency
    con.execute("INSERT INTO instruments(symbol, exchange, uic, asset_type, currency, resolved_at) "
               "VALUES ('MSFT','xnas',1,'Stock','EUR','t')")           # e.g. a CFD variant resolved in EUR
    assert ledger.ticker_currency(cfg, con, "MSFT") == "EUR"           # resolved instrument wins
    assert ledger.ticker_currency(cfg, con, "NOPE") == cfg.benchmark.currency   # unknown ticker: safe fallback


def test_known_rates_carries_forward_the_latest_rate(cfg):
    con = connect(cfg.storage.db_path)
    seed_fx(con, "USD", 1.00, day="2026-09-01")
    seed_fx(con, "USD", 1.05, day="2026-09-10")
    assert ledger.known_rates(con, "2026-09-05") == {"USD": 1.00}      # before the second rate
    assert ledger.known_rates(con, "2026-09-15") == {"USD": 1.05}      # carried forward
    assert ledger.known_rates(con, "2026-08-01") == {}                 # before any known rate


# ---------- the actual money math, end to end ----------

async def test_a_usd_fill_is_converted_to_eur_at_fill_time(cfg, monkeypatch):
    """The bug this fixes: a USD position summed unconverted into a EUR cash balance."""
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    router, _ = make_router(cfg, monkeypatch, [good_view(f), good_view(f)], [proposal(fields=f, weight=0.10)])
    saxo = make_saxo(today=D0, fx_mid=1.10)          # 1 EUR = 1.10 USD, so 1 USD = 0.90909... EUR
    await cli.run_once(cfg, con, saxo_inproc=saxo, llm=router, today=D0, decide=True, log=lambda *_: None)

    rate_row = con.execute("SELECT rate FROM fx_rates WHERE currency='USD' AND date=?", (D0.isoformat(),)).fetchone()
    assert rate_row is not None and rate_row["rate"] == pytest.approx(1 / 1.10)

    saxo2 = make_saxo(today=D1, fx_mid=1.10)
    await cli.run_once(cfg, con, saxo_inproc=saxo2, llm=router, today=D1, log=lambda *_: None)
    fill = con.execute("SELECT * FROM fills WHERE ticker='MSFT'").fetchone()
    assert fill["currency"] == "USD" and fill["fx_rate"] == pytest.approx(1 / 1.10)
    native_value = fill["quantity"] * fill["price"]
    assert fill["value"] == pytest.approx(native_value / 1.10, rel=1e-6)          # EUR, not the raw USD number
    assert fill["value"] != pytest.approx(native_value)                          # would be the bug: no conversion

    # cash() and book() sum fills.value directly - correct now precisely because it is EUR already
    b = ledger.book(cfg, con, {"MSFT": fill["price"]}, D1)
    expected_cash = cfg.benchmark.start_capital - fill["value"] - fill["fee"]
    assert b["cash"] == pytest.approx(expected_cash, abs=0.01)
    assert b["positions"]["MSFT"]["currency"] == "USD"
    assert b["positions"]["MSFT"]["value"] == pytest.approx(fill["quantity"] * fill["price"] / 1.10, rel=1e-6)
    assert b["total_value"] == pytest.approx(b["cash"] + b["positions"]["MSFT"]["value"], abs=0.01)
    # weight is computed against the correctly-converted total, matching the 0.10 the trader asked for
    assert b["positions"]["MSFT"]["weight"] == pytest.approx(0.10, abs=0.01)


async def test_no_fx_rate_leaves_the_decision_pending_not_mispriced(cfg):
    con = connect(cfg.storage.db_path)
    con.execute("INSERT INTO runs(run_id, started_at, status, environment, config_hash) VALUES ('r0','t','ok','SIM','h')")
    pid = con.execute("INSERT INTO trader_proposals(run_id, ticker, action, target_weight, winning_argument, rejected_json, "
                      "stop_condition, source, created_at) VALUES ('r0','MSFT','long',0.1,'x','[]','','trader','t')").lastrowid
    vid = con.execute("INSERT INTO risk_verdicts(proposal_id, rules_checked_json, verdict, original_weight, adjusted_weight, numbers_json) "
                      "VALUES (?,'[]','pass',0.1,0.1,'{}')", (pid,)).lastrowid
    d = con.execute("INSERT INTO decisions(run_id, ticker, proposal_id, verdict_id, action, final_weight, created_at, status, source) "
                    "VALUES ('r0','MSFT',?,?,'long',0.1,'t','pending','trader')", (pid, vid)).lastrowid
    filled = ledger.fill_pending(cfg, con, "r1", {"MSFT": 100.0}, D1, log=lambda *_: None)
    assert filled == []
    assert con.execute("SELECT status FROM decisions WHERE id=?", (d,)).fetchone()["status"] == "pending"
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


async def test_fx_outage_warns_but_does_not_fail_the_run(cfg, monkeypatch):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    router, _ = make_router(cfg, monkeypatch, [good_view(f)], [proposal(fields=f)])
    saxo = make_saxo(today=D0, fx_broken=True)
    run_id = await cli.run_once(cfg, con, saxo_inproc=saxo, llm=router, today=D0, decide=True, log=lambda *_: None)
    r = con.execute("SELECT status, warnings FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert r["status"] == "ok" and "fx rate unavailable" in r["warnings"] and "USD->EUR" in r["warnings"]
    # the decision was still proposed and gated; it just never fills without a rate
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


async def test_eur_only_universe_never_touches_fx(cfg):
    """No foreign currency in the universe: fx.snapshot fetches nothing, no extra Saxo calls."""
    cfg.universe.tickers = [Ticker(symbol="SXR8", mic="xetr", currency="EUR")]     # matches the fake's EUR instrument
    cfg.pipeline.analysts = []
    con = connect(cfg.storage.db_path)
    calls = []
    saxo = make_saxo(today=D0, calls=calls, fx_broken=True)      # would raise if fx were fetched at all
    await cli.run_once(cfg, con, saxo_inproc=saxo, today=D0, decide=False, log=lambda *_: None)
    assert not any(name == "search_instruments" and args.get("assetTypes") == "FxSpot" for name, args in calls)
    assert con.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0


# ---------- desk/performance.py: recompute also converts ----------

def test_performance_recompute_converts_held_usd_position(cfg):
    con = connect(cfg.storage.db_path)
    for i, (day, px, bv) in enumerate([("2026-09-14", 100.0, 100000.0), ("2026-09-15", 100.0, 100000.0),
                                       ("2026-09-16", 110.0, 100000.0)]):
        con.execute("INSERT INTO runs(run_id, started_at, status, environment, config_hash, run_date) VALUES (?,?,?,?,?,?)",
                    (f"r{i}", f"{day}T20:30:00", "ok", "SIM", "h", day))
        con.execute("INSERT INTO data_packs(run_id, ticker, stable_json, stable_hash, volatile_json, created_at) VALUES (?,?,?,?,?,?)",
                    (f"r{i}", "MSFT", json.dumps({"bars": [{"close": px}]}), "x", json.dumps({"quote": {"mid": px}}), f"{day}T20:30:01"))
        con.execute("INSERT INTO benchmark_snapshots(date, symbol, currency, price, units, value) VALUES (?,?,?,?,?,?)",
                    (day, "SXR8", "EUR", bv / 1000, 1000, bv))
    pid = con.execute("INSERT INTO trader_proposals(run_id, ticker, action, target_weight, winning_argument, rejected_json, stop_condition) "
                      "VALUES ('r0','MSFT','long',0.1,'x','[]','')").lastrowid
    vid = con.execute("INSERT INTO risk_verdicts(proposal_id, rules_checked_json, verdict, original_weight, adjusted_weight, numbers_json) "
                      "VALUES (?,'[]','pass',0.1,0.1,'{}')", (pid,)).lastrowid
    con.execute("INSERT INTO decisions(run_id, ticker, proposal_id, verdict_id, action, final_weight, created_at, status) "
                "VALUES ('r0','MSFT',?,?,'long',0.1,'2026-09-14T20:31:00','filled')", (pid, vid))
    con.execute("INSERT INTO fills(decision_id, source, ticker, quantity, price, filled_at, fee, value, run_id, currency, fx_rate) "
                "VALUES (1,'shadow','MSFT',100,100.0,'2026-09-15T20:30:02',5,9090.91,'r1','USD',0.909091)")
    seed_fx(con, "USD", 0.909091, day="2026-09-14")
    seed_fx(con, "USD", 0.86, day="2026-09-16")           # the dollar weakened: fewer EUR per USD by day 3

    s = performance.compute(cfg, con)
    assert s.discrepancies == []
    last = s.rows[-1]
    expected_cash = cfg.benchmark.start_capital - 9090.91 - 5
    expected_value = expected_cash + 100 * 110.0 * 0.86
    assert last.shadow == pytest.approx(expected_value, abs=0.5)
    assert not any("valued without conversion" in n for n in s.notes)
