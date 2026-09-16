import math

from desk import indicators


def bars(closes, vol=1000.0):
    return [{"date": f"2026-01-{i+1:02d}", "open": c, "high": None if c is None else c + 1,
             "low": None if c is None else c - 1, "close": c, "volume": vol}
            for i, c in enumerate(closes)]


def test_sma_and_returns():
    b = bars([float(x) for x in range(1, 31)])   # 1..30
    ind = indicators.compute(b)
    assert ind["bars_available"] == 30 and ind["as_of"] == "2026-01-30"
    assert ind["last_close"] == 30.0
    assert ind["sma_20"] == 20.5                  # mean of 11..30
    assert ind["sma_50"] is None and ind["sma_200"] is None
    assert ind["return_5d"] == round(30 / 25 - 1, 4)
    assert ind["return_60d"] is None
    assert ind["high_252d"] == 30.0 and ind["low_252d"] == 1.0
    assert ind["pct_from_high_252d"] == 0.0


def test_rsi_extremes_and_atr():
    up = bars([float(x) for x in range(1, 20)])
    assert indicators.compute(up)["rsi_14"] == 100.0
    down = bars([float(x) for x in range(20, 1, -1)])
    assert indicators.compute(down)["rsi_14"] == 0.0
    # high-low = 2 every day and |gap| = 1, so true range is max(2, 2, 0) = 2 -> ATR 2
    assert indicators.compute(up)["atr_14"] == 2.0
    assert indicators.compute(up)["atr_14_pct"] == round(2 / 19, 4)


def test_deterministic_and_handles_missing_values():
    b = bars([10.0, 11.0, 12.0, None, 13.0])
    a1, a2 = indicators.compute(b), indicators.compute(b)
    assert a1 == a2 and a1["bars_available"] == 4
    assert indicators.compute([]) == {"bars_available": 0}
    flat = bars([5.0] * 30)
    assert indicators.compute(flat)["realized_vol_20d"] == 0.0
    assert indicators.compute(flat)["max_drawdown_60d"] == 0.0
