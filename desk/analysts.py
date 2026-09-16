"""Analyst step: data pack in, validated AnalystView out. One role in phase 1.

Each attempt is recorded in llm_calls whether or not it validated. A view is
stored in analyst_views only if it is schema-valid AND every evidence item
resolves to a real field of the data pack with the right value.
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
from .schemas import AnalystView, check_evidence, flatten

OUTPUT_CONTRACT = """Output a single JSON object and nothing else, with exactly these keys:
{
  "stance": "favourable" | "unfavourable" | "neutral",
  "thesis": string (20-900 chars),
  "evidence": [ {"field": string, "value": number|string|boolean|null, "why": string}, ... ] (2 to 8 items),
  "confidence": number between 0 and 1,
  "would_be_wrong_if": string (10-400 chars),
  "horizon_days": integer between 5 and 365
}"""

EVIDENCE_RULES = """Rules, all strict:
1. Use only the FIELDS block. It is the complete set of facts you have. Do not use outside knowledge of the company, news, or the macro picture.
2. Every item in "evidence" must copy a key from FIELDS into "field" and copy its value EXACTLY into "value". Do not compute, round or invent numbers. To argue from a comparison, cite both fields as separate evidence items and make the comparison in "why".
3. "would_be_wrong_if" must be a falsifiable condition stated in terms of one or more FIELDS keys.
4. Be specific and short. No hedging language, no disclaimers."""

TECHNICAL_SYSTEM = f"""You are the technical analyst on a small research desk. The desk decides on a fixed cadence and holds for weeks to months (long only, no shorts). Your single question: is the current ENTRY TIMING for this instrument acceptable now, and what would make that view wrong?

{EVIDENCE_RULES}
5. {OUTPUT_CONTRACT}"""

FUNDAMENTALS_SYSTEM = f"""You are the fundamentals analyst on a small research desk. The desk decides on a fixed cadence and holds for weeks to months (long only, no shorts). Your single question: over a 6-12 month horizon, is this business worth owning at today's valuation, judged against its own history and its peers, and what would make that view wrong? Entry timing is not your job; another analyst covers it.

{EVIDENCE_RULES}
5. {OUTPUT_CONTRACT}"""

# prompt key -> (system prompt, default pack sections the role sees)
PROMPTS: dict[str, tuple[str, list[str]]] = {
    "technical": (TECHNICAL_SYSTEM, ["ticker", "instrument", "indicators", "bars_last_20"]),
    "fundamentals": (FUNDAMENTALS_SYSTEM, ["ticker", "instrument", "fundamentals", "indicators_summary"]),
}


def pack_view(stable: dict[str, Any], sections: list[str]) -> dict[str, Any]:
    """Select the parts of the stable pack a role may see."""
    ind = stable.get("indicators", {})
    available: dict[str, Any] = {
        "ticker": stable.get("ticker", {}),
        "instrument": {k: stable.get("instrument", {}).get(k) for k in ("description", "currency", "saxo_symbol")},
        "indicators": ind,
        "indicators_summary": {k: ind.get(k) for k in ("as_of", "last_close", "return_60d", "return_250d",
                                                       "pct_from_high_252d", "realized_vol_20d")},
        "bars_last_20": stable.get("bars", [])[-20:],
        "fundamentals": stable.get("fundamentals", {}),
    }
    return {k: available[k] for k in sections if k in available}


def role_prompt(cfg: Config, role_name: str) -> tuple[str, list[str]]:
    role = cfg.roles[role_name]
    key = role.prompt or "technical"
    if key not in PROMPTS and not role.prompt_file:
        raise ValueError(f"role {role_name!r}: unknown prompt {key!r}; known {sorted(PROMPTS)} or set prompt_file")
    system, sections = PROMPTS.get(key, ("", []))
    if role.prompt_file:
        system = cfg.path(role.prompt_file).read_text()
    if role.sections:
        sections = role.sections
    return system, sections


def build_prompt(cfg: Config, role_name: str, stable: dict[str, Any], as_of: str) -> tuple[str, str, dict[str, Any]]:
    """Return (system, user, fields). `fields` is what evidence is checked against."""
    system, sections = role_prompt(cfg, role_name)
    view = pack_view(stable, sections)
    fields = flatten(view)
    user = (
        f"DATE: {as_of}\n"
        f"INSTRUMENT: {stable.get('ticker', {}).get('symbol')} ({stable.get('instrument', {}).get('description')})\n\n"
        "FIELDS (key: value). Cite keys verbatim.\n"
        + "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
        + "\n\nAnswer with the JSON object only."
    )
    return system, user, fields


def technical_prompt(stable: dict[str, Any], as_of: str) -> tuple[str, str, dict[str, Any]]:
    """Kept for `desk prompt` and tests: the technical prompt with default sections."""
    system, sections = PROMPTS["technical"]
    view = pack_view(stable, sections)
    fields = flatten(view)
    user = (
        f"DATE: {as_of}\n"
        f"INSTRUMENT: {stable.get('ticker', {}).get('symbol')} ({stable.get('instrument', {}).get('description')})\n\n"
        "FIELDS (key: value). Cite keys verbatim.\n"
        + "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
        + "\n\nAnswer with the JSON object only."
    )
    return system, user, fields


def run_view(cfg: Config, llm: LlmClient, con: sqlite3.Connection, run_id: str, ticker: str,
             stable: dict[str, Any], as_of: str, role_name: str = "technical_analyst",
             log=print) -> int | None:
    """Produce and store one analyst view. Returns the analyst_views id, or None if no
    valid view could be obtained within cfg.pipeline.max_attempts."""
    log = as_log(log)
    role = cfg.roles[role_name]
    system, user, fields = build_prompt(cfg, role_name, stable, as_of)
    feedback = ""
    for attempt in range(1, cfg.pipeline.max_attempts + 1):
        user_msg = user if not feedback else user + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED:\n" + feedback + "\nFix every problem and answer again with the JSON object only."
        error: str | None = None
        resp = None
        try:
            resp = llm.complete(role, system, user_msg, AnalystView)
        except LlmError as e:
            error = f"llm: {e}"
        view: AnalystView | None = None
        if resp is not None:
            try:
                obj = parse_json_object(resp.text)
                view = AnalystView.model_validate(obj)
                problems = check_evidence(view, fields)
                if problems:
                    error = "evidence: " + "; ".join(problems)
                    view = None
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
            log.cost(role_name, resp.model, resp.tokens_in, resp.tokens_out, resp.cost_usd, resp.latency_ms, attempt, view is not None)
        if view is not None:
            vid = con.execute(
                "INSERT INTO analyst_views(run_id, ticker, role, thesis, evidence_json, confidence, would_be_wrong_if, "
                "llm_call_id, stance, horizon_days) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, ticker, role_name, view.thesis, j([e.model_dump() for e in view.evidence]), view.confidence,
                 view.would_be_wrong_if, call_id, view.stance, view.horizon_days),
            ).lastrowid
            con.commit()
            log(f"{ticker}: {role_name} {view.stance} (conf {view.confidence:.2f}, {len(view.evidence)} evidence, attempt {attempt})")
            log.block(f"{role_name} on {ticker}: {view.stance}, confidence {view.confidence:.2f}, horizon {view.horizon_days}d",
                      [f"thesis: {view.thesis}", "evidence:"]
                      + [f"  - {e.field} = {e.value}: {e.why}" for e in view.evidence]
                      + [f"wrong if: {view.would_be_wrong_if}"])
            return vid
        log(f"{ticker}: {role_name} attempt {attempt} rejected: {error[:160] if error else '?'}")
        log.detail(f"  rejected answer ({role_name}, attempt {attempt}): {(resp.text if resp else '')[:600]}")
        feedback = error or ""
        if error and error.startswith("llm:"):
            break  # provider failure: retries already happened inside the client
    return None
