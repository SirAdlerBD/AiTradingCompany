"""Inter-agent messages. Analysts return JSON validated against these; never free text.

The evidence rule is the whole point of phase 1: every evidence item must name a
field of the data pack and quote its value exactly. `check_evidence` enforces
that against the flattened pack, so a hallucinated number cannot pass.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Stance = Literal["favourable", "unfavourable", "neutral"]


class Evidence(BaseModel):
    field: str = Field(min_length=1, description="exact FIELDS key from the data pack")
    value: float | int | str | bool | None
    why: str = Field(min_length=3, max_length=300)


class AnalystView(BaseModel):
    stance: Stance
    thesis: str = Field(min_length=20, max_length=900)
    evidence: list[Evidence] = Field(min_length=2, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    would_be_wrong_if: str = Field(min_length=10, max_length=400)
    horizon_days: int = Field(ge=5, le=365)

    @field_validator("thesis", "would_be_wrong_if")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def flatten(obj: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    """{'indicators': {'sma_50': 1.0}, 'bars': [{'close': 2}]} -> {'indicators.sma_50': 1.0, 'bars[0].close': 2}"""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or str(a).lower() == str(b).lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        return math.isclose(float(a), float(b), rel_tol=5e-3, abs_tol=1e-4)
    if isinstance(a, (int, float)) and isinstance(b, str):
        try:
            return _same(a, float(b))
        except ValueError:
            return False
    if isinstance(b, (int, float)) and isinstance(a, str):
        return _same(b, a)
    if a is None or b is None:
        return a is None and (b is None or str(b).lower() in ("null", "none"))
    return str(a).strip() == str(b).strip()


def check_evidence(view: AnalystView, fields: dict[str, Any]) -> list[str]:
    """Return a list of problems; empty means every evidence item resolves and matches."""
    problems = []
    for i, e in enumerate(view.evidence):
        if e.field not in fields:
            problems.append(f"evidence[{i}].field {e.field!r} is not a FIELDS key")
            continue
        if not _same(fields[e.field], e.value):
            problems.append(f"evidence[{i}].value {e.value!r} does not match FIELDS[{e.field!r}] = {fields[e.field]!r}")
    return problems


Action = Literal["long", "hold", "exit", "none"]


class Stop(BaseModel):
    """A stop the code can evaluate every day: FIELDS[field] <op> value."""
    field: str = Field(min_length=1, description="a FIELDS key with a numeric value")
    op: Literal["<", ">"]
    value: float

    def triggered(self, fields: dict[str, Any]) -> bool | None:
        v = fields.get(self.field)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        return v < self.value if self.op == "<" else v > self.value


class RejectedArgument(BaseModel):
    role: str = Field(min_length=1)
    argument: str = Field(min_length=3, max_length=300)
    why_rejected: str = Field(min_length=3, max_length=300)


class TraderProposal(BaseModel):
    action: Action
    target_weight: float = Field(ge=0.0, le=1.0, description="target share of portfolio value; 0 for exit/none")
    winning_argument: str = Field(min_length=10, max_length=900)
    rejected_arguments: list[RejectedArgument] = Field(default_factory=list, max_length=8)
    sided_with: list[str] = Field(default_factory=list, description="analyst roles whose view carried the decision")
    stop: Stop | None = None
    horizon_days: int = Field(ge=5, le=365)
    confidence: float = Field(ge=0.0, le=1.0)


def check_proposal(p: TraderProposal, fields: dict[str, Any], has_position: bool, analyst_roles: list[str]) -> list[str]:
    """Structural problems a schema cannot express. Empty list = accepted."""
    problems = []
    if p.action in ("long", "hold"):
        if p.target_weight <= 0:
            problems.append(f"action {p.action} needs target_weight > 0")
        if p.stop is None:
            problems.append(f"action {p.action} needs a stop")
    if p.action in ("exit", "none") and p.target_weight != 0:
        problems.append(f"action {p.action} needs target_weight 0")
    if p.action in ("hold", "exit") and not has_position:
        problems.append(f"action {p.action} but there is no open position; use long or none")
    if p.stop is not None:
        if p.stop.field not in fields:
            problems.append(f"stop.field {p.stop.field!r} is not a FIELDS key")
        elif not isinstance(fields[p.stop.field], (int, float)) or isinstance(fields[p.stop.field], bool):
            problems.append(f"stop.field {p.stop.field!r} is not numeric")
        elif p.stop.triggered(fields):
            problems.append(f"stop {p.stop.field} {p.stop.op} {p.stop.value} is already triggered today "
                            f"(value {fields[p.stop.field]})")
    unknown = [r for r in p.sided_with if r not in analyst_roles]
    if unknown:
        problems.append(f"sided_with names unknown analyst(s) {unknown}; known: {analyst_roles}")
    return problems
