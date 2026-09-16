"""desk performance: recomputed from fills + frozen quotes, benchmark rebased, honest below min days."""
import json
from datetime import date, timedelta

import pytest

from desk import performance
from desk.cli import cmd_performance
from desk.db import connect, j

START = date(2026, 9, 14)


def seed(con, cfg, n_days, shadow_px, bench_val, first_decision_day=0, buy_day=1, qty=100.0, snapshots=True):
    """n_days of ok runs. One decision on first_decision_day, filled on buy_day at that day's mark."""
    for i in range(n_days):
        d = (START + timedelta(days=i)).isoformat()
        rid = f"r{i}"
        con.execute("INSERT INTO runs(run_id, started_at, status, environment, config_hash, run_date) VALUES (?,?,?,?,?,?)",
                    (rid, f"{d}T20:30:00", "ok", "SIM", "h", d))
        con.execute("INSERT INTO data_packs(run_id, ticker, stable_json, stable_hash, volatile_json, created_at) VALUES (?,?,?,?,?,?)",
                    (rid, "MSFT", j({"bars": [{"close": shadow_px[i]}]}), "x", j({"quote": {"mid": shadow_px[i]}}), f"{d}T20:30:01"))
        con.execute("INSERT INTO benchmark_snapshots(date, symbol, currency, price, units, value) VALUES (?,?,?,?,?,?)",
                    (d, "SXR8", "EUR", bench_val[i] / 1000, 1000, bench_val[i]))
        if i == first_decision_day:
            pid = con.execute("INSERT INTO trader_proposals(run_id, ticker, action, target_weight, winning_argument, rejected_json, stop_condition) "
                              "VALUES (?,?,?,?,?,?,?)", (rid, "MSFT", "long", 0.1, "x", "[]", "")).lastrowid
            vid = con.execute("INSERT INTO risk_verdicts(proposal_id, rules_checked_json, verdict, original_weight, adjusted_weight, numbers_json) "
                              "VALUES (?,?,?,?,?,?)", (pid, "[]", "pass", 0.1, 0.1, "{}")).lastrowid
            con.execute("INSERT INTO decisions(run_id, ticker, proposal_id, verdict_id, action, final_weight, created_at, status) "
                        "VALUES (?,?,?,?,?,?,?,?)", (rid, "MSFT", pid, vid, "long", 0.1, f"{d}T20:31:00", "filled"))
        if i == buy_day:
            px = shadow_px[i]
            con.execute("INSERT INTO fills(decision_id, source, ticker, quantity, price, filled_at, fee, value, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (1, "shadow", "MSFT", qty, px, f"{d}T20:30:02", qty * px * 5 / 1e4, qty * px, rid))
        if snapshots:
            held = qty if i >= buy_day else 0.0
            spent = (qty * shadow_px[buy_day] * (1 + 5 / 1e4)) if i >= buy_day else 0.0
            total = cfg.benchmark.start_capital - spent + held * shadow_px[i]
            con.execute("INSERT INTO portfolio_snapshots(date, cash, positions_json, total_value) VALUES (?,?,?,?)",
                        (d, cfg.benchmark.start_capital - spent, "{}", total))
    con.commit()


def test_series_rebased_delta_and_tally(cfg):
    con = connect(cfg.storage.db_path)
    px = [100, 100, 102, 101, 105, 103, 104, 106, 108, 107, 110, 111]        # MSFT marks per day
    bv = [100000, 100500, 100800, 100600, 101000, 101500, 101300, 101800, 102000, 102500, 102400, 103000]
    seed(con, cfg, 12, px, bv)
    s = performance.compute(cfg, con)
    assert s.start == "2026-09-14" and s.start_reason == "first decision" and s.trading_days == 11
    r0, r1, r2 = s.rows[0], s.rows[1], s.rows[2]
    assert r0.shadow == 100000 and r0.bench == 100000 and r0.ahead is None and r0.delta_pp == 0
    # day 1: bought 100 @ 100 with 0.05 fee -> 99999.95; benchmark +0.5% -> shadow behind
    assert r1.shadow == pytest.approx(100000 - 5.0) and r1.bench == pytest.approx(100500) and r1.ahead is False
    assert r1.tally_behind == 1 and r1.tally_ahead == 0
    # day 2: 100 shares @102 = +200 on 99995 -> +0.195%; bench +0.8% -> still behind
    assert r2.shadow == pytest.approx(100195.0) and r2.delta_pp == pytest.approx((100195 / 100000 - 1 - 0.008) * 100)
    last = s.rows[-1]
    assert last.shadow == pytest.approx(100000 - 5 + 100 * 11)                   # 101095
    assert last.bench_ret == pytest.approx(0.03) and last.delta_pp == pytest.approx((0.01095 - 0.03) * 100)
    assert last.tally_ahead + last.tally_behind == 11 and last.positions == 1
    assert s.discrepancies == [] and all("skipped" not in n for n in s.notes)


def test_since_rebases_both_series_to_that_day(cfg):
    con = connect(cfg.storage.db_path)
    seed(con, cfg, 6, [100, 100, 110, 110, 121, 121], [100000, 100000, 100000, 100000, 110000, 110000])
    s = performance.compute(cfg, con, since="2026-09-16")
    assert s.start_reason == "--since" and s.rows[0].date == "2026-09-16"
    assert s.rows[0].shadow_ret == 0 and s.rows[0].bench_ret == 0
    assert s.rows[-1].bench_ret == pytest.approx(0.10)                 # 100000 -> 110000 after the start
    # cash after the buy is 100000 - 10000 - 5 = 89995; shadow 09-16 = 89995 + 100*110, last = 89995 + 100*121
    assert s.rows[-1].shadow_ret == pytest.approx((89995 + 12100) / (89995 + 11000) - 1)


def test_snapshot_mismatch_is_flagged_and_missing_price_carried(cfg):
    con = connect(cfg.storage.db_path)
    seed(con, cfg, 4, [100, 100, 105, 105], [100000] * 4)
    con.execute("UPDATE portfolio_snapshots SET total_value = total_value + 50 WHERE date='2026-09-16'")
    con.execute("DELETE FROM data_packs WHERE run_id='r3'")                 # no quote on the last day
    con.commit()
    s = performance.compute(cfg, con)
    assert any("2026-09-16: recomputed" in d for d in s.discrepancies)
    assert s.rows[-1].shadow == s.rows[-2].shadow                        # carried at the last known mark


def test_too_early_banner_and_no_chart(cfg, capsys, tmp_path):
    cfg.storage.reports_dir = tmp_path / "reports"
    con = connect(cfg.storage.db_path)
    seed(con, cfg, 3, [100, 100, 130], [100000, 100000, 100000])
    con.close()
    assert cmd_performance(cfg, min_days=10, since=None, chart="auto", force_chart=False) == 0
    out = capsys.readouterr().out
    assert "TOO EARLY: 2 trading day(s) of data, 10 needed" in out and "chart skipped" in out
    # 100 shares bought at 100 (fee 5), marked at 130: 89995 + 13000 = 102995 -> +2.995% vs a flat benchmark
    assert "DELTA vs SXR8" in out and "102995.00" in out and "+2.99 pp" in out or "+3.00 pp" in out
    assert "nothing annualised" in out
    assert not (tmp_path / "reports").exists() or not list((tmp_path / "reports").glob("performance-*"))


def test_nothing_to_show_without_decisions(cfg, capsys):
    con = connect(cfg.storage.db_path)
    con.execute("INSERT INTO runs(run_id, started_at, status, environment, config_hash, run_date) VALUES ('r','t','ok','SIM','h','2026-09-14')")
    con.commit(); con.close()
    cmd_performance(cfg, 10, None, "none", False)
    assert "no decisions yet" in capsys.readouterr().out


def test_chart_png_and_svg_written_with_date_in_name(cfg, tmp_path, capsys):
    cfg.storage.reports_dir = tmp_path / "reports"
    con = connect(cfg.storage.db_path)
    px = [100 + i for i in range(12)]
    seed(con, cfg, 12, px, [100000 + 100 * i for i in range(12)])
    s = performance.compute(cfg, con)
    p = performance.render_chart(cfg, s, backend="png")
    assert p.name == "performance-2026-09-25.png" and p.stat().st_size > 5000
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    q = performance.render_chart(cfg, s, backend="svg")
    txt = q.read_text()
    assert q.name == "performance-2026-09-25.svg" and "<svg" in txt and "shadow book" in txt and "SXR8" in txt
    assert '#2a78d6' in txt and '#eb6834' in txt
    con.close()
    cmd_performance(cfg, 10, None, "auto", False)
    out = capsys.readouterr().out
    assert "chart: " in out and "TOO EARLY" not in out
