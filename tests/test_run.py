"""End-to-end phase 0 against the in-process fake saxo-mcp."""
from datetime import date

import pytest

from desk import cli, guard
from desk.db import connect
from tests.conftest import SIM_KEY, make_saxo

TODAY = date(2026, 9, 16)


async def test_two_same_day_runs_hash_identically(cfg):
    con = connect(cfg.storage.db_path)
    calls: list = []
    saxo = make_saxo(today=TODAY, calls=calls)
    r1 = await cli.run_once(cfg, con, saxo_inproc=saxo, today=TODAY, log=lambda *_: None)
    r2 = await cli.run_once(cfg, con, saxo_inproc=saxo, today=TODAY, log=lambda *_: None)

    rows = con.execute("SELECT run_id, stable_hash, volatile_json FROM data_packs ORDER BY created_at").fetchall()
    assert [r["run_id"] for r in rows] == [r1, r2]
    assert rows[0]["stable_hash"] == rows[1]["stable_hash"]          # the exit criterion
    assert rows[0]["volatile_json"] != rows[1]["volatile_json"]      # quotes differ, hash does not

    runs = con.execute("SELECT status, account_key FROM runs ORDER BY started_at").fetchall()
    assert [r["status"] for r in runs] == ["ok", "ok"]
    assert runs[0]["account_key"] == SIM_KEY

    # today's bar is excluded from the pack; price_history holds the rest
    n = con.execute("SELECT COUNT(*), MAX(date) FROM price_history WHERE ticker='MSFT'").fetchone()
    assert n[0] == cfg.universe.history_days - 1 and n[1] == "2026-09-15"

    # benchmark: one row per day, units fixed on day 0, ETF resolved on XETR in EUR
    b = con.execute("SELECT * FROM benchmark_snapshots").fetchall()
    assert len(b) == 1 and b[0]["symbol"] == "SXR8" and b[0]["currency"] == "EUR"
    assert b[0]["units"] == pytest.approx(cfg.benchmark.start_capital / b[0]["price"], rel=1e-3)

    # instruments are resolved once and cached: 2 searches total, not 4
    assert sum(1 for c in calls if c[0] == "search_instruments") == 2
    inst = con.execute("SELECT uic, asset_type FROM instruments WHERE symbol='MSFT'").fetchone()
    assert (inst["uic"], inst["asset_type"]) == (1234, "Stock")
    # the decoys on another venue / with a near-miss ticker were not picked
    bench = con.execute("SELECT uic, asset_type FROM instruments WHERE symbol='SXR8'").fetchone()
    assert (bench["uic"], bench["asset_type"]) == (9876, "Etf")
    # and no server-side exchangeId filter was sent (a wrong one returns nothing)
    assert all(c[1]["exchangeId"] is None for c in calls if c[0] == "search_instruments")

    # chart call used saxo-mcp's argument names
    chart = next(c[1] for c in calls if c[0] == "get_chart_data")
    assert chart == {"uic": 1234, "assetType": "Stock", "horizon": 1440, "count": cfg.universe.history_days}


async def test_report_flags_nothing_after_clean_runs(cfg, capsys):
    con = connect(cfg.storage.db_path)
    saxo = make_saxo(today=TODAY)
    await cli.run_once(cfg, con, saxo_inproc=saxo, today=TODAY, log=lambda *_: None)
    con.close()
    assert cli.cmd_report(cfg) == 0
    assert "MISMATCH" not in capsys.readouterr().out


async def test_guard_blocks_trading_enabled_server(cfg):
    con = connect(cfg.storage.db_path)
    saxo = make_saxo(today=TODAY, trading="ENABLED (SAXO_TRADING=enabled): order tools are available.")
    with pytest.raises(guard.GuardFailure, match="trading"):
        await cli.run_once(cfg, con, saxo_inproc=saxo, today=TODAY, log=lambda *_: None)
    assert con.execute("SELECT status FROM runs").fetchone()["status"] == "guard_failed"
    assert con.execute("SELECT COUNT(*) FROM data_packs").fetchone()[0] == 0


async def test_guard_blocks_unknown_account(cfg):
    con = connect(cfg.storage.db_path)
    saxo = make_saxo(today=TODAY, account_keys=(SIM_KEY, "SomeOtherAcc"))
    with pytest.raises(guard.GuardFailure, match="not in SIM allowlist"):
        await cli.run_once(cfg, con, saxo_inproc=saxo, today=TODAY, log=lambda *_: None)


async def test_guard_blocks_live_env(cfg, monkeypatch):
    monkeypatch.setenv("SAXO_ALLOW_LIVE", "1")
    con = connect(cfg.storage.db_path)
    with pytest.raises(guard.GuardFailure, match="forbidden env var"):
        await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), today=TODAY, log=lambda *_: None)


def test_schema_rejects_non_sim_run(cfg):
    import sqlite3
    con = connect(cfg.storage.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO runs(run_id, started_at, status, environment, config_hash) VALUES ('x','t','ok','LIVE','h')")
