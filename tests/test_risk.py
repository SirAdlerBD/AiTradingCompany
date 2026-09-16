from pathlib import Path

import pytest

from desk import risk
from desk.risk import BookContext, dd_halted, evaluate, load_rules, pearson

RULES = load_rules(Path(__file__).parent.parent / "config" / "risk_rules.yaml")


def ctx(**over):
    base = dict(total_value=100000.0, cash=100000.0, weights={}, sectors={}, drawdown=0.0, dd_halted=False,
                turnover_window=0.0, returns={}, days_held={})
    base.update(over)
    return BookContext(**base)


def test_rules_load_with_version_and_unknown_id_rejected(tmp_path):
    assert RULES.version == 2 and RULES.get("MAX_POSITION_PCT").limit == 0.15
    bad = tmp_path / "r.yaml"
    bad.write_text("version: 1\nrules:\n  - id: MAX_LEVERAGE\n    limit: 2\n")
    with pytest.raises(ValueError, match="unknown rule id"):
        load_rules(bad)


def test_pass_within_limits():
    v = evaluate(RULES, "long", "MSFT", 0.10, ctx())
    assert v.verdict == "pass" and v.adjusted_weight == 0.10 and v.rule_fired is None
    assert "MAX_POSITION_PCT" in v.rules_checked


def test_resize_to_max_position():
    v = evaluate(RULES, "long", "MSFT", 0.40, ctx())
    assert v.verdict == "resize" and v.rule_fired == "MAX_POSITION_PCT" and v.adjusted_weight == 0.15
    assert v.numbers["MAX_POSITION_PCT_limit"] == 0.15


def test_min_cash_resizes_when_book_is_full():
    c = ctx(weights={"AAPL": 0.15, "NVDA": 0.15, "GOOGL": 0.15, "AMZN": 0.15, "META": 0.15, "ORCL": 0.13})
    v = evaluate(RULES, "long", "MSFT", 0.15, c)                 # 0.88 invested; room = 1 - 0.10 - 0.88 = 0.02
    assert v.verdict == "resize" and v.rule_fired == "MIN_CASH_PCT" and v.adjusted_weight == pytest.approx(0.02)


def test_sector_limit_uses_others_in_sector():
    c = ctx(weights={"AAPL": 0.15, "NVDA": 0.15}, sectors={"AAPL": "Technology", "NVDA": "Technology"})
    v = evaluate(RULES, "long", "MSFT", 0.15, c, candidate_sector="Technology")
    assert v.verdict == "resize" and v.rule_fired == "MAX_SECTOR_PCT" and v.adjusted_weight == pytest.approx(0.05)
    v2 = evaluate(RULES, "long", "MSFT", 0.15, c, candidate_sector=None)
    assert v2.verdict == "pass" and v2.numbers["MAX_SECTOR_PCT"].startswith("not evaluated")


def test_drawdown_halt_vetoes_new_longs_but_not_exits_or_holds():
    c = ctx(drawdown=-0.13, dd_halted=True, weights={"MSFT": 0.10})
    assert evaluate(RULES, "long", "AAPL", 0.10, c).verdict == "veto"
    v = evaluate(RULES, "long", "MSFT", 0.10, c)              # not larger than current: allowed
    assert v.verdict == "pass"
    assert evaluate(RULES, "exit", "MSFT", 0.0, c).verdict == "pass"


def test_turnover_veto():
    v = evaluate(RULES, "long", "MSFT", 0.15, ctx(turnover_window=0.25))
    assert v.verdict == "veto" and v.rule_fired == "MAX_WEEKLY_TURNOVER" and v.adjusted_weight == 0.0


def test_correlation_veto():
    up = [0.01 * ((i % 3) - 1) for i in range(60)]
    c = ctx(weights={"AAPL": 0.10}, returns={"AAPL": up, "MSFT": up})
    v = evaluate(RULES, "long", "MSFT", 0.10, c)
    assert v.verdict == "veto" and v.rule_fired == "MAX_CORR_TO_BOOK" and v.numbers["corr_to_book"] == 1.0
    assert pearson([1, 2, 3], [1, 1, 1]) is None


def test_dd_halt_hysteresis():
    vals = [100, 95, 87, 90, 91, 93, 95]        # dd hits -13% then recovers to -5%
    assert dd_halted(vals[:3], 0.12, 0.08) == (True, pytest.approx(-0.13))
    assert dd_halted(vals[:5], 0.12, 0.08)[0] is True           # -9%: still halted
    assert dd_halted(vals, 0.12, 0.08)[0] is False              # -5%: resumed
    assert risk.time_stop_days(RULES) == 120
