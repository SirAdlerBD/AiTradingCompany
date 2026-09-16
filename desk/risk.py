"""Deterministic risk layer. No model anywhere in this file.

Rules live in config/risk_rules.yaml with stable ids. Each verdict cites the
rule that fired and the numbers it saw, nothing else. Exits are never vetoed:
the rules only constrain new or larger exposure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

KNOWN_RULES = {
    "MAX_POSITION_PCT", "MAX_SECTOR_PCT", "MIN_CASH_PCT", "MAX_CORR_TO_BOOK",
    "PORTFOLIO_DD_HALT", "MAX_WEEKLY_TURNOVER", "MAX_HOLDING_DAYS", "LONG_ONLY",
}


@dataclass
class Rule:
    id: str
    on_breach: str                      # resize | veto | exit
    limit: float | None = None
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""


@dataclass
class RuleSet:
    version: int
    rules: list[Rule]

    def get(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id and r.enabled), None)

    def summary(self) -> list[str]:
        out = []
        for r in self.rules:
            if r.enabled:
                lim = "" if r.limit is None else f" limit {r.limit}"
                extra = " ".join(f"{k}={v}" for k, v in r.params.items())
                out.append(f"{r.id}{lim} {extra} -> {r.on_breach}".strip())
        return out


def load_rules(path: Path) -> RuleSet:
    doc = yaml.safe_load(Path(path).read_text())
    rules = []
    for r in doc.get("rules", []):
        rid = r["id"]
        if rid not in KNOWN_RULES:
            raise ValueError(f"risk_rules.yaml: unknown rule id {rid!r}; known {sorted(KNOWN_RULES)}")
        params = {k: v for k, v in r.items() if k not in ("id", "on_breach", "limit", "enabled", "description")}
        rules.append(Rule(id=rid, on_breach=r.get("on_breach", "veto"), limit=r.get("limit"),
                          params=params, enabled=bool(r.get("enabled", True)), description=r.get("description", "")))
    return RuleSet(version=int(doc.get("version", 1)), rules=rules)


@dataclass
class BookContext:
    """Everything the rules need, computed by the ledger. Weights are fractions of total value."""
    total_value: float
    cash: float
    weights: dict[str, float]                    # ticker -> weight
    sectors: dict[str, str | None]               # ticker -> sector (None if unknown)
    drawdown: float                              # current, <= 0
    dd_halted: bool                              # from PORTFOLIO_DD_HALT hysteresis
    turnover_window: float                       # sum |fill value| in window / total value
    returns: dict[str, list[float]]              # ticker -> recent daily returns (for correlation)
    days_held: dict[str, int]


@dataclass
class Verdict:
    verdict: str                                 # pass | resize | veto
    rule_fired: str | None
    original_weight: float
    adjusted_weight: float
    rules_checked: list[str]
    numbers: dict[str, Any]


def evaluate(rules: RuleSet, action: str, ticker: str, target_weight: float, ctx: BookContext,
             candidate_sector: str | None = None) -> Verdict:
    checked: list[str] = []
    numbers: dict[str, Any] = {"action": action, "target_weight": target_weight}
    original = target_weight
    weight = target_weight
    current = ctx.weights.get(ticker, 0.0)

    if action in ("exit", "none"):
        return Verdict("pass", None, original, 0.0, ["(exits and no-ops are never vetoed)"], numbers)

    def resize_to(rule: Rule, new_weight: float, extra: dict[str, Any]) -> Verdict | None:
        nonlocal weight
        numbers.update(extra)
        if rule.on_breach == "veto":
            return Verdict("veto", rule.id, original, current if action == "hold" else 0.0, checked, numbers)
        weight = max(0.0, min(weight, new_weight))
        numbers[f"{rule.id}_resized_to"] = round(weight, 6)
        return None

    r = rules.get("LONG_ONLY")
    if r:
        checked.append(r.id)
        if action == "short":
            return Verdict("veto", r.id, original, 0.0, checked, numbers)

    r = rules.get("PORTFOLIO_DD_HALT")
    if r:
        checked.append(r.id)
        numbers["drawdown"] = round(ctx.drawdown, 6)
        numbers["dd_halted"] = ctx.dd_halted
        if ctx.dd_halted and weight > current:
            if r.on_breach == "veto":
                return Verdict("veto", r.id, original, current, checked, numbers)
            weight = current

    r = rules.get("MAX_POSITION_PCT")
    if r and r.limit is not None:
        checked.append(r.id)
        if weight > r.limit:
            v = resize_to(r, r.limit, {"MAX_POSITION_PCT_limit": r.limit})
            if v:
                return v

    r = rules.get("MAX_SECTOR_PCT")
    if r and r.limit is not None:
        checked.append(r.id)
        if candidate_sector is None:
            numbers["MAX_SECTOR_PCT"] = "not evaluated: sector unknown"
        else:
            others = sum(w for t, w in ctx.weights.items() if t != ticker and ctx.sectors.get(t) == candidate_sector)
            room = r.limit - others
            numbers["sector"] = candidate_sector
            numbers["sector_exposure_others"] = round(others, 6)
            if weight > room:
                v = resize_to(r, room, {"MAX_SECTOR_PCT_limit": r.limit})
                if v:
                    return v

    r = rules.get("MIN_CASH_PCT")
    if r and r.limit is not None:
        checked.append(r.id)
        invested_others = sum(w for t, w in ctx.weights.items() if t != ticker)
        room = 1.0 - r.limit - invested_others
        numbers["cash_room_for_ticker"] = round(room, 6)
        if weight > room:
            v = resize_to(r, room, {"MIN_CASH_PCT_limit": r.limit})
            if v:
                return v

    r = rules.get("MAX_WEEKLY_TURNOVER")
    if r and r.limit is not None:
        checked.append(r.id)
        trade = abs(weight - current)
        numbers["turnover_window"] = round(ctx.turnover_window, 6)
        numbers["trade_size"] = round(trade, 6)
        if ctx.turnover_window + trade > r.limit and trade > 0:
            if r.on_breach == "veto":
                return Verdict("veto", r.id, original, current, checked, numbers)
            weight = current + max(0.0, r.limit - ctx.turnover_window) * (1 if weight > current else -1)

    r = rules.get("MAX_CORR_TO_BOOK")
    if r and r.limit is not None and weight > current:
        checked.append(r.id)
        c = corr_to_book(ticker, ctx)
        numbers["corr_to_book"] = None if c is None else round(c, 4)
        if c is not None and c > r.limit:
            if r.on_breach == "veto":
                return Verdict("veto", r.id, original, current, checked, numbers)
            weight = current

    if weight <= 0 and action == "long":
        return Verdict("veto", numbers.get("last_resize_rule") or checked[-1] if checked else None,
                       original, 0.0, checked, numbers)
    verdict = "pass" if math.isclose(weight, original, abs_tol=1e-9) else "resize"
    fired = None
    if verdict == "resize":
        fired = next((k[:-len("_resized_to")] for k in numbers if k.endswith("_resized_to")), None)
    return Verdict(verdict, fired, original, round(weight, 6), checked, numbers)


def corr_to_book(ticker: str, ctx: BookContext) -> float | None:
    """Correlation of the candidate's daily returns with the weighted book's returns."""
    cand = ctx.returns.get(ticker)
    book = {t: w for t, w in ctx.weights.items() if t != ticker and w > 0 and t in ctx.returns}
    if not cand or not book:
        return None
    n = min([len(cand)] + [len(ctx.returns[t]) for t in book])
    if n < 20:
        return None
    tot = sum(book.values())
    br = [sum(ctx.returns[t][-n:][i] * w / tot for t, w in book.items()) for i in range(n)]
    cr = cand[-n:]
    return pearson(cr, br)


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def dd_halted(values: list[float], limit: float, resume_at: float) -> tuple[bool, float]:
    """Hysteresis on the drawdown series: halt once dd exceeds `limit`, resume once it recovers above `resume_at`.
    Returns (halted, current_drawdown)."""
    halted, peak, dd = False, 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        dd = v / peak - 1 if peak > 0 else 0.0
        if -dd > limit:
            halted = True
        elif halted and -dd < resume_at:
            halted = False
    return halted, dd


def time_stop_days(rules: RuleSet) -> int | None:
    r = rules.get("MAX_HOLDING_DAYS")
    return int(r.limit) if r and r.limit is not None else None
