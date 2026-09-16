from datetime import date

import pytest

from desk import ledger
from desk.db import connect, now


def seed_decision(con, ticker, action, weight, source="trader"):
    con.execute("INSERT OR IGNORE INTO runs(run_id, started_at, status, environment, config_hash) VALUES ('r0','t','ok','SIM','h')")
    pid = con.execute("INSERT INTO trader_proposals(run_id, ticker, action, target_weight, winning_argument, rejected_json, "
                      "stop_condition, source, created_at) VALUES ('r0',?,?,?,'x','[]','',?,?)",
                      (ticker, action, weight, source, now())).lastrowid
    vid = con.execute("INSERT INTO risk_verdicts(proposal_id, rules_checked_json, verdict, original_weight, adjusted_weight, numbers_json) "
                      "VALUES (?,'[]','pass',?,?,'{}')", (pid, weight, weight)).lastrowid
    return con.execute("INSERT INTO decisions(run_id, ticker, proposal_id, verdict_id, action, final_weight, created_at, status, source) "
                       "VALUES ('r0',?,?,?,?,?,?,'pending',?)", (ticker, pid, vid, action, weight, now(), source)).lastrowid


def test_fill_buy_then_exit_with_fees(cfg):
    con = connect(cfg.storage.db_path)
    d1 = seed_decision(con, "MSFT", "long", 0.10)
    filled = ledger.fill_pending(cfg, con, "r1", {"MSFT": 100.0}, date(2026, 9, 17), log=lambda *_: None)
    assert len(filled) == 1 and filled[0]["quantity"] == pytest.approx(100.0)      # 10% of 100k at 100
    assert filled[0]["fee"] == pytest.approx(10000 * 5 / 1e4)
    assert con.execute("SELECT status FROM decisions WHERE id=?", (d1,)).fetchone()["status"] == "filled"
    b = ledger.book(cfg, con, {"MSFT": 110.0})
    assert b["positions"]["MSFT"]["quantity"] == pytest.approx(100.0)
    assert b["cash"] == pytest.approx(100000 - 10000 - 5)
    assert b["total_value"] == pytest.approx(100000 - 10005 + 11000)
    assert b["positions"]["MSFT"]["unrealized_pct"] == pytest.approx(11000 / 10005 - 1, abs=1e-4)

    seed_decision(con, "MSFT", "exit", 0.0, source="stop")
    ledger.fill_pending(cfg, con, "r2", {"MSFT": 110.0}, date(2026, 9, 18), log=lambda *_: None)
    b = ledger.book(cfg, con, {"MSFT": 110.0})
    assert b["positions"] == {} and b["cash"] == pytest.approx(100000 - 10005 + 11000 - 5.5)
    snap = ledger.snapshot(cfg, con, "r2", {}, date(2026, 9, 18))
    assert snap["total_value"] == pytest.approx(b["cash"]) and snap["drawdown"] == 0.0


def test_pending_without_price_stays_pending_and_noop_below_one_unit(cfg):
    con = connect(cfg.storage.db_path)
    d = seed_decision(con, "MSFT", "long", 0.10)
    assert ledger.fill_pending(cfg, con, "r1", {}, date(2026, 9, 17), log=lambda *_: None) == []
    assert con.execute("SELECT status FROM decisions WHERE id=?", (d,)).fetchone()["status"] == "pending"
    d2 = seed_decision(con, "SXR8", "long", 0.000001)
    ledger.fill_pending(cfg, con, "r1", {"SXR8": 100.0, "MSFT": 100.0}, date(2026, 9, 17), log=lambda *_: None)
    assert con.execute("SELECT status FROM decisions WHERE id=?", (d2,)).fetchone()["status"] == "noop"


def test_resize_to_target_weight_sells_the_excess(cfg):
    con = connect(cfg.storage.db_path)
    seed_decision(con, "MSFT", "long", 0.10)
    ledger.fill_pending(cfg, con, "r1", {"MSFT": 100.0}, date(2026, 9, 17), log=lambda *_: None)
    seed_decision(con, "MSFT", "long", 0.05)
    f = ledger.fill_pending(cfg, con, "r2", {"MSFT": 100.0}, date(2026, 9, 18), log=lambda *_: None)
    assert f[0]["quantity"] < 0 and abs(f[0]["quantity"]) == pytest.approx(50, rel=0.01)


def test_drawdown_and_peak_tracking(cfg):
    con = connect(cfg.storage.db_path)
    seed_decision(con, "MSFT", "long", 0.15)
    ledger.fill_pending(cfg, con, "r1", {"MSFT": 100.0}, date(2026, 9, 17), log=lambda *_: None)
    s1 = ledger.snapshot(cfg, con, "r1", {"MSFT": 100.0}, date(2026, 9, 17))
    s2 = ledger.snapshot(cfg, con, "r2", {"MSFT": 80.0}, date(2026, 9, 18))
    assert s1["drawdown"] == pytest.approx(-7.5 / 100000, abs=1e-6)     # only the fee
    assert s2["drawdown"] == pytest.approx((100000 - 7.5 - 3000) / 100000 - 1, abs=1e-6)
