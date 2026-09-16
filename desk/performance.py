"""`desk performance`: did the shadow book beat the index?

Recomputed from the tables every time, never from a live quote:
  - positions and cash from `fills` up to each day,
  - marks from the quote frozen in that day's data pack (the same hashed
    inputs the analysts saw), carried forward when a held ticker has no quote,
  - the benchmark from `benchmark_snapshots`, rebased to the comparison start.
Both series start from the same value on the start date (the day of the first
decision by default), so the delta between their cumulative returns is the
answer. Nothing here is annualised or extrapolated.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import Config

# Reference palette (dataviz skill): categorical slots 1 and 2, light surface, text inks.
SHADOW_COLOR = "#2a78d6"
BENCH_COLOR = "#eb6834"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e6e5e2"


@dataclass
class Row:
    date: str
    shadow: float
    bench: float                 # benchmark rebased to the shadow start value
    shadow_ret: float
    bench_ret: float
    delta_pp: float              # (shadow_ret - bench_ret) * 100, percentage points
    ahead: bool | None           # None on the start day
    tally_ahead: int
    tally_behind: int
    positions: int


@dataclass
class Series:
    rows: list[Row] = field(default_factory=list)
    start: str | None = None
    start_reason: str = ""
    symbol: str = ""
    currency: str = ""
    trading_days: int = 0         # days after the start day
    notes: list[str] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)


def _mark_from_pack(volatile: dict[str, Any], stable: dict[str, Any], mark: str) -> float | None:
    q = (volatile or {}).get("quote") or {}
    for k in (mark, "mid", "last"):
        if q.get(k) is not None:
            return float(q[k])
    bars = (stable or {}).get("bars") or []
    if bars and bars[-1].get("close") is not None:
        return float(bars[-1]["close"])
    return None


def compute(cfg: Config, con: sqlite3.Connection, since: str | None = None) -> Series:
    s = Series()
    days = con.execute(
        "SELECT run_id, run_date FROM runs WHERE status='ok' AND run_date IS NOT NULL "
        "AND run_id IN (SELECT run_id FROM (SELECT run_id, run_date, MAX(started_at) FROM runs WHERE status='ok' GROUP BY run_date)) "
        "ORDER BY run_date"
    ).fetchall()
    if not days:
        s.notes.append("no successful runs with a run_date yet")
        return s
    if since:
        s.start, s.start_reason = since, "--since"
    else:
        first = con.execute(
            "SELECT r.run_date FROM decisions d JOIN runs r USING(run_id) WHERE r.run_date IS NOT NULL ORDER BY r.run_date LIMIT 1"
        ).fetchone()
        if not first:
            s.notes.append("no decisions yet: the shadow book has never held anything, so there is nothing to compare")
            return s
        s.start, s.start_reason = first["run_date"], "first decision"
    days = [d for d in days if d["run_date"] >= s.start]
    if not days:
        s.notes.append(f"no successful runs on or after {s.start}")
        return s

    bench = {r["date"]: (float(r["value"]), r["symbol"], r["currency"]) for r in
             con.execute("SELECT date, value, symbol, currency FROM benchmark_snapshots")}
    snaps = {r["date"]: float(r["total_value"]) for r in con.execute("SELECT date, total_value FROM portfolio_snapshots")}
    start_cap = cfg.benchmark.start_capital
    last_price: dict[str, float] = {}
    shadow0 = bench0 = None
    ahead = behind = 0

    for d in days:
        day, run_id = d["run_date"], d["run_id"]
        for r in con.execute("SELECT ticker, stable_json, volatile_json FROM data_packs WHERE run_id=?", (run_id,)):
            px = _mark_from_pack(json.loads(r["volatile_json"] or "{}"), json.loads(r["stable_json"] or "{}"), cfg.ledger.mark)
            if px is not None:
                last_price[r["ticker"]] = px
        qty: dict[str, float] = {}
        spent = 0.0
        for f in con.execute("SELECT ticker, quantity, value, COALESCE(fee,0) AS fee FROM fills WHERE substr(filled_at,1,10) <= ?", (day,)):
            qty[f["ticker"]] = qty.get(f["ticker"], 0.0) + float(f["quantity"])
            spent += float(f["value"]) + float(f["fee"])
        cash = start_cap - spent
        value = cash
        npos = 0
        for t, q in qty.items():
            if abs(q) < 1e-9:
                continue
            npos += 1
            if t not in last_price:
                s.notes.append(f"{day}: no price ever seen for held ticker {t}; valued at 0")
                continue
            value += q * last_price[t]
        if day in snaps and abs(snaps[day] - value) > 0.01:
            s.discrepancies.append(f"{day}: recomputed {value:.2f} vs snapshot {snaps[day]:.2f}")
        if day not in bench:
            s.notes.append(f"{day}: no benchmark snapshot, day skipped")
            continue
        bv, sym, cur = bench[day]
        s.symbol, s.currency = sym, cur or ""
        if shadow0 is None:
            shadow0, bench0 = value, bv
        sret = value / shadow0 - 1
        bret = bv / bench0 - 1
        delta = (sret - bret) * 100
        is_start = len(s.rows) == 0
        a = None if is_start else delta > 0
        if a is True:
            ahead += 1
        elif a is False and delta < 0:
            behind += 1
        s.rows.append(Row(day, round(value, 2), round(shadow0 * (1 + bret), 2), sret, bret, delta, a, ahead, behind, npos))
    s.trading_days = max(0, len(s.rows) - 1)
    return s


def render_table(s: Series, min_days: int) -> str:
    out: list[str] = []
    if not s.rows:
        out.append("performance: nothing to show yet")
        out.extend(f"  {n}" for n in s.notes)
        return "\n".join(out)
    last = s.rows[-1]
    head = (f"DELTA vs {s.symbol} since {s.start} ({s.start_reason}), {s.trading_days} trading day(s):  "
            f"{last.delta_pp:+.2f} pp   ahead {last.tally_ahead} day(s) / behind {last.tally_behind}")
    bar = "=" * len(head)
    out += [bar, head, bar]
    if s.trading_days < min_days:
        out.append(f"TOO EARLY: {s.trading_days} trading day(s) of data, {min_days} needed before this number means anything.")
        out.append("The table is shown for the record; do not read a trend into it.")
        out.append(bar)
    out.append(f"{'date':10s} {'shadow':>12s} {'bench':>12s} {'shadow %':>9s} {'bench %':>9s}   {'DELTA pp':>10s}  {'ahead/behind':>13s} {'pos':>3s}")
    for r in s.rows:
        mark = "  " if r.ahead is None else ("▲ " if r.ahead else ("▼ " if r.delta_pp < 0 else "= "))
        out.append(f"{r.date:10s} {r.shadow:12.2f} {r.bench:12.2f} {r.shadow_ret * 100:+8.2f}% {r.bench_ret * 100:+8.2f}%   "
                   f"{mark}{r.delta_pp:+8.2f}  {r.tally_ahead:>6d}/{r.tally_behind:<6d} {r.positions:>3d}")
    out.append(bar)
    out.append(f"shadow {last.shadow_ret * 100:+.2f}% vs {s.symbol} {last.bench_ret * 100:+.2f}% over the period; "
               f"values in {s.currency or 'account currency'}; cumulative only, nothing annualised.")
    for n in s.notes:
        out.append(f"note: {n}")
    for d in s.discrepancies:
        out.append(f"WARNING snapshot mismatch: {d}")
    return "\n".join(out)


def chart_path(cfg: Config, s: Series, ext: str) -> Path:
    d = cfg.storage.reports_dir or cfg.storage.db_path.parent / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"performance-{s.rows[-1].date}.{ext}"


def render_chart(cfg: Config, s: Series, backend: str = "auto") -> Path:
    """PNG via matplotlib when installed (extra `charts`), else a hand-written SVG with no dependency."""
    if backend in ("auto", "png"):
        try:
            os.environ.setdefault("MPLCONFIGDIR", str((cfg.storage.reports_dir or cfg.storage.db_path.parent / "reports") / ".mpl"))
            Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            return _png(cfg, s, plt)
        except ImportError:
            if backend == "png":
                raise
    return _svg(cfg, s)


def _png(cfg: Config, s: Series, plt) -> Path:
    xs = list(range(len(s.rows)))
    labels = [r.date[5:] for r in s.rows]
    sv = [r.shadow for r in s.rows]
    bv = [r.bench for r in s.rows]
    last = s.rows[-1]
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=120)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.plot(xs, sv, color=SHADOW_COLOR, linewidth=2, solid_joinstyle="round", solid_capstyle="round", label="shadow book")
    ax.plot(xs, bv, color=BENCH_COLOR, linewidth=2, solid_joinstyle="round", solid_capstyle="round", label=f"{s.symbol} benchmark")
    for ys, col, name in ((sv, SHADOW_COLOR, "shadow book"), (bv, BENCH_COLOR, f"{s.symbol}")):
        ax.plot([xs[-1]], [ys[-1]], marker="o", markersize=8, color=col, markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(f"{name}  {ys[-1]:,.0f}", (xs[-1], ys[-1]), xytext=(8, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)
    ax.grid(True, axis="y", color=GRID, linewidth=1)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8)
    step = max(1, len(xs) // 10)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels(labels[::step])
    ax.set_ylabel(f"value ({s.currency})", color=INK_2, fontsize=9)
    ax.set_title(f"Shadow book vs {s.symbol} since {s.start}", loc="left", fontsize=12, color=INK)
    verdict = f"delta {last.delta_pp:+.2f} pp after {s.trading_days} trading day(s); ahead {last.tally_ahead}, behind {last.tally_behind}"
    fig.text(0.01, 0.01, verdict, fontsize=9, color=INK_2)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0.04, 0.9, 1))
    p = chart_path(cfg, s, "png")
    fig.savefig(p, facecolor=SURFACE)
    plt.close(fig)
    return p


def _svg(cfg: Config, s: Series) -> Path:
    W, H, L, R, T, B = 1000, 520, 70, 200, 50, 50
    sv = [r.shadow for r in s.rows]
    bv = [r.bench for r in s.rows]
    lo, hi = min(sv + bv), max(sv + bv)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    n = len(s.rows)

    def x(i: int) -> float:
        return L + (W - L - R) * (i / max(1, n - 1))

    def y(v: float) -> float:
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    def path(vals: list[float]) -> str:
        return " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))

    last = s.rows[-1]
    grid = ""
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        grid += f'<line x1="{L}" y1="{y(v):.1f}" x2="{W - R}" y2="{y(v):.1f}" stroke="{GRID}" stroke-width="1"/>'
        grid += f'<text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="{INK_2}">{v:,.0f}</text>'
    step = max(1, n // 10)
    xt = "".join(f'<text x="{x(i):.1f}" y="{H - B + 18}" text-anchor="middle" font-size="11" fill="{INK_2}">{s.rows[i].date[5:]}</text>'
                 for i in range(0, n, step))
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="sans-serif">
<rect width="{W}" height="{H}" fill="{SURFACE}"/>
<text x="{L}" y="28" font-size="16" fill="{INK}">Shadow book vs {s.symbol} since {s.start}</text>
{grid}{xt}
<path d="{path(sv)}" fill="none" stroke="{SHADOW_COLOR}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
<path d="{path(bv)}" fill="none" stroke="{BENCH_COLOR}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
<circle cx="{x(n - 1):.1f}" cy="{y(sv[-1]):.1f}" r="5" fill="{SHADOW_COLOR}" stroke="{SURFACE}" stroke-width="2"/>
<circle cx="{x(n - 1):.1f}" cy="{y(bv[-1]):.1f}" r="5" fill="{BENCH_COLOR}" stroke="{SURFACE}" stroke-width="2"/>
<text x="{x(n - 1) + 10:.1f}" y="{y(sv[-1]) + 4:.1f}" font-size="12" fill="{INK}">shadow book {sv[-1]:,.0f}</text>
<text x="{x(n - 1) + 10:.1f}" y="{y(bv[-1]) + 4:.1f}" font-size="12" fill="{INK}">{s.symbol} {bv[-1]:,.0f}</text>
<text x="{L}" y="{H - 12}" font-size="12" fill="{INK_2}">delta {last.delta_pp:+.2f} pp after {s.trading_days} trading day(s); ahead {last.tally_ahead}, behind {last.tally_behind}</text>
</svg>
"""
    p = chart_path(cfg, s, "svg")
    p.write_text(svg)
    return p
