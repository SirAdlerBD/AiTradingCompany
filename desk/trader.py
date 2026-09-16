"""Trader step: pack + analyst views + book + risk limits in, TraderProposal out.

The trader is the only role that weighs arguments. It sees what the analysts
said, what the book holds, and the risk limits it will be held to, and must
name the winning argument, the rejected ones, and a stop the code can check.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from .config import Config
from .db import j, now
from .log import as_log
from .llm import LlmClient, LlmError, parse_json_object, prompt_hash
from .risk import RuleSet
from .schemas import TraderProposal, check_proposal, flatten
from .analysts import pack_view

TRADER_SYSTEM = """You are the trader on a small research desk. Long only, no leverage. The desk decides on a fixed cadence and holds for weeks to months. You are given the frozen facts about one instrument (FIELDS), the analysts' views on it, the current book, and the risk limits that code will enforce after you. Your job is to decide for this instrument only.

Rules:
1. Weigh the analysts' arguments; do not average them. Name the argument that wins and why each rejected argument loses. If the analysts agree, say what would still make them both wrong.
2. Every stop must be a FIELDS key with a numeric value, an operator, and a level that is NOT already breached today. Code evaluates it daily and exits when it triggers. Prefer stops anchored to a level in FIELDS (for example indicators.sma_200 or indicators.low_252d) over arbitrary percentages.
3. Respect the risk limits when choosing target_weight; a proposal above a limit is resized or vetoed by code and counts against you.
4. "hold" and "exit" are only valid when the book has a position in this instrument. "none" means no position and no buy.
5. Do not use outside knowledge, news, or the macro picture. FIELDS and the views are all you have.
6. Be specific and short. No hedging language, no disclaimers.

Output a single JSON object and nothing else:
{
  "action": "long" | "hold" | "exit" | "none",
  "target_weight": number in [0, 1] (0 for exit/none),
  "winning_argument": string (10-900 chars),
  "rejected_arguments": [ {"role": string, "argument": string, "why_rejected": string}, ... ],
  "sided_with": [analyst role names whose view carried the decision],
  "stop": {"field": string, "op": "<" | ">", "value": number} or null,
  "horizon_days": integer 5-365,
  "confidence": number in [0, 1]
}"""

TRADER_SECTIONS = ["ticker", "instrument", "indicators", "fundamentals"]


def build_prompt(cfg: Config, stable: dict[str, Any], as_of: str, views: list[dict[str, Any]],
                 book: dict[str, Any], ticker: str, rules: RuleSet, last: dict[str, Any] | None,
                 analyst_roles: list[str]) -> tuple[str, str, dict[str, Any]]:
    role = cfg.roles[cfg.pipeline.trader]
    system = cfg.path(role.prompt_file).read_text() if role.prompt_file else TRADER_SYSTEM
    sections = role.sections or TRADER_SECTIONS
    fields = flatten(pack_view(stable, sections))
    pos = book["positions"].get(ticker)
    book_lines = [f"total_value: {book['total_value']}", f"cash: {book['cash']} ({book['cash'] / book['total_value']:.1%})"]
    for t, r in sorted(book["positions"].items()):
        book_lines.append(f"{t}: weight {r['weight']:.3f}, unrealized {r['unrealized_pct']:+.1%}, opened {r['opened_at']}")
    this_pos = (f"position in {ticker}: weight {pos['weight']:.3f}, unrealized {pos['unrealized_pct']:+.1%}, opened {pos['opened_at']}"
                if pos else f"no position in {ticker}")
    view_blocks = []
    for v in views:
        ev = "; ".join(f"{e['field']}={e['value']} ({e['why']})" for e in json.loads(v["evidence_json"]))
        view_blocks.append(f"[{v['role']}] stance={v['stance']} confidence={v['confidence']:.2f} horizon={v['horizon_days']}d\n"
                           f"  thesis: {v['thesis']}\n  evidence: {ev}\n  wrong if: {v['would_be_wrong_if']}")
    last_line = (f"last decision on {ticker}: {last['action']} weight {last['final_weight']} on {last['created_at'][:10]}, "
                 f"risk verdict {last['verdict']}" + (f" ({last['rule_fired']})" if last['rule_fired'] else "")) if last else \
                f"no previous decision on {ticker}"
    user = (
        f"DATE: {as_of}\nINSTRUMENT: {ticker} ({stable.get('instrument', {}).get('description')})\n\n"
        f"BOOK\n" + "\n".join(book_lines) + f"\n{this_pos}\n{last_line}\n\n"
        f"RISK LIMITS (enforced by code)\n" + "\n".join(rules.summary()) + "\n\n"
        f"ANALYST VIEWS ({len(views)}; roles: {', '.join(analyst_roles)})\n" + ("\n".join(view_blocks) if view_blocks else "(none)") + "\n\n"
        "FIELDS (key: value). Stops must cite these keys.\n"
        + "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
        + "\n\nAnswer with the JSON object only."
    )
    return system, user, fields


def propose(cfg: Config, llm: LlmClient, con: sqlite3.Connection, run_id: str, ticker: str,
            stable: dict[str, Any], as_of: str, book: dict[str, Any], rules: RuleSet, log=print) -> int | None:
    """Ask the trader, validate, store. Returns trader_proposals.id or None."""
    log = as_log(log)
    role_name = cfg.pipeline.trader
    role = cfg.roles[role_name]
    views = con.execute(
        "SELECT * FROM analyst_views WHERE ticker=? AND run_id IN (SELECT run_id FROM runs WHERE status IN ('ok','running')) "
        "AND id IN (SELECT MAX(id) FROM analyst_views WHERE ticker=? GROUP BY role) ORDER BY role", (ticker, ticker)
    ).fetchall()
    last = con.execute(
        "SELECT d.action, d.final_weight, d.created_at, v.verdict, v.rule_fired FROM decisions d "
        "JOIN risk_verdicts v ON v.id=d.verdict_id WHERE d.ticker=? ORDER BY d.id DESC LIMIT 1", (ticker,)
    ).fetchone()
    analyst_roles = list(cfg.pipeline.analysts)
    system, user, fields = build_prompt(cfg, stable, as_of, [dict(v) for v in views], book, ticker, rules,
                                        dict(last) if last else None, analyst_roles)
    has_position = ticker in book["positions"]
    feedback = ""
    for attempt in range(1, cfg.pipeline.max_attempts + 1):
        user_msg = user if not feedback else user + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED:\n" + feedback + "\nFix every problem and answer again with the JSON object only."
        error: str | None = None
        resp = None
        proposal: TraderProposal | None = None
        try:
            resp = llm.complete(role, system, user_msg, TraderProposal)
        except LlmError as e:
            error = f"llm: {e}"
        if resp is not None:
            try:
                proposal = TraderProposal.model_validate(parse_json_object(resp.text))
                problems = check_proposal(proposal, fields, has_position, analyst_roles)
                if problems:
                    error = "proposal: " + "; ".join(problems)
                    proposal = None
            except (ValueError, ValidationError) as e:
                error = f"schema: {str(e)[:800]}"
        call_id = con.execute(
            "INSERT INTO llm_calls(run_id, ticker, role, model, prompt_hash, prompt, response, tokens_in, tokens_out, "
            "latency_ms, cost_usd, created_at, attempt, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ticker, role_name, resp.model if resp else role.model, prompt_hash(system, user_msg),
             system + "\n\n---\n\n" + user_msg, resp.text if resp else "", resp.tokens_in if resp else None,
             resp.tokens_out if resp else None, resp.latency_ms if resp else None, resp.cost_usd if resp else None,
             now(), attempt, error),
        ).lastrowid
        con.commit()
        if resp is not None:
            log.cost(role_name, resp.model, resp.tokens_in, resp.tokens_out, resp.cost_usd, resp.latency_ms, attempt, proposal is not None)
        if proposal is not None:
            pid = con.execute(
                "INSERT INTO trader_proposals(run_id, ticker, action, target_weight, winning_argument, rejected_json, "
                "stop_condition, llm_call_id, sided_with_json, stop_json, horizon_days, confidence, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, ticker, proposal.action, proposal.target_weight, proposal.winning_argument,
                 j([r.model_dump() for r in proposal.rejected_arguments]),
                 f"{proposal.stop.field} {proposal.stop.op} {proposal.stop.value}" if proposal.stop else "",
                 call_id, j(proposal.sided_with), j(proposal.stop.model_dump()) if proposal.stop else None,
                 proposal.horizon_days, proposal.confidence, "trader", now()),
            ).lastrowid
            con.commit()
            log(f"{ticker}: trader {proposal.action} w={proposal.target_weight:.3f} sided_with={proposal.sided_with} "
                f"stop={proposal.stop.field + proposal.stop.op + str(proposal.stop.value) if proposal.stop else '-'} (attempt {attempt})")
            log.block(f"trader on {ticker}: {proposal.action} target weight {proposal.target_weight:.3f}, "
                      f"confidence {proposal.confidence:.2f}, horizon {proposal.horizon_days}d",
                      [f"views weighed: {', '.join(v['role'] + '=' + v['stance'] for v in views) or 'none'}",
                       f"sided with: {', '.join(proposal.sided_with) or 'nobody'}",
                       f"winning argument: {proposal.winning_argument}"]
                      + ([f"rejected [{r.role}]: {r.argument} -> {r.why_rejected}" for r in proposal.rejected_arguments]
                         or ["rejected: none"])
                      + [f"stop: {proposal.stop.field} {proposal.stop.op} {proposal.stop.value}" if proposal.stop else "stop: none"])
            return pid
        log(f"{ticker}: trader attempt {attempt} rejected: {error[:160] if error else '?'}")
        log.detail(f"  rejected answer (trader, attempt {attempt}): {(resp.text if resp else '')[:600]}")
        feedback = error or ""
        if error and error.startswith("llm:"):
            break
    return None


def synthetic_proposal(con: sqlite3.Connection, run_id: str, ticker: str, source: str, reason: str) -> int:
    """A code-generated exit proposal (stop or time stop) so every decision has a proposal behind it."""
    pid = con.execute(
        "INSERT INTO trader_proposals(run_id, ticker, action, target_weight, winning_argument, rejected_json, "
        "stop_condition, llm_call_id, sided_with_json, stop_json, horizon_days, confidence, source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, ticker, "exit", 0.0, reason, "[]", "", None, "[]", None, None, None, source, now()),
    ).lastrowid
    con.commit()
    return pid
