"""Deterministic technical indicators computed in code from daily bars.

Pure Python, no numpy: the values land in the stable data pack and must be
bit-for-bit reproducible across runs. Everything is rounded to 4 decimals.
Returns None for any indicator that lacks enough history rather than guessing.
"""
from __future__ import annotations

import math
from typing import Any

R = 4


def _r(x: float | None) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, R)


def sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(bars: list[dict[str, Any]], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    """Annualised standard deviation of daily log returns over the last n days."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    m = sum(rets) / n
    var = sum((x - m) ** 2 for x in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


def max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    return mdd


def compute(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """bars: sorted ascending, each {date, open, high, low, close, volume}, no partial bar."""
    clean = [b for b in bars if b.get("close") is not None and b.get("high") is not None and b.get("low") is not None]
    closes = [float(b["close"]) for b in clean]
    vols = [float(b["volume"]) for b in clean if b.get("volume") is not None]
    if not closes:
        return {"bars_available": 0}
    last = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    hi252 = max(closes[-252:])
    lo252 = min(closes[-252:])

    def ret(n: int) -> float | None:
        return closes[-1] / closes[-1 - n] - 1 if len(closes) > n else None

    def pct_vs(ref: float | None) -> float | None:
        return last / ref - 1 if ref else None

    avg_vol_20 = sma(vols, 20) if len(vols) >= 20 else None
    out = {
        "bars_available": len(clean),
        "as_of": clean[-1]["date"],
        "last_close": last,
        "sma_20": s20, "sma_50": s50, "sma_200": s200,
        "pct_vs_sma_20": pct_vs(s20), "pct_vs_sma_50": pct_vs(s50), "pct_vs_sma_200": pct_vs(s200),
        "sma_50_above_sma_200": (s50 > s200) if (s50 is not None and s200 is not None) else None,
        "return_5d": ret(5), "return_20d": ret(20), "return_60d": ret(60), "return_120d": ret(120), "return_250d": ret(250),
        "high_252d": hi252, "low_252d": lo252,
        "pct_from_high_252d": last / hi252 - 1, "pct_from_low_252d": last / lo252 - 1,
        "rsi_14": rsi(closes, 14),
        "atr_14": atr(clean, 14),
        "atr_14_pct": None,
        "realized_vol_20d": realized_vol(closes, 20),
        "max_drawdown_60d": max_drawdown(closes[-60:]),
        "avg_volume_20d": avg_vol_20,
        "volume_last_vs_avg_20d": (vols[-1] / avg_vol_20) if (avg_vol_20 and vols) else None,
    }
    if out["atr_14"] is not None:
        out["atr_14_pct"] = out["atr_14"] / last
    return {k: (_r(v) if isinstance(v, float) else v) for k, v in out.items()}
