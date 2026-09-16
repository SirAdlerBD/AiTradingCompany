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
from .llm import LlmClient, LlmError, parse_json_object, prompt_hash
from .schemas import AnalystView, check_evidence, flatten

TECHNICAL_SYSTEM = """You are the technical analyst on a small research desk. The desk decides weekly and holds for weeks to months (long only, no shorts). Your single question: is the current ENTRY TIMING for this instrument acceptable this week, and what would make that view wrong?

Rules, all strict:
1. Use only the FIELDS block. It is the complete set of facts you have. Do not use outside knowledge of the company, news, or the macro picture; that is another analyst's job.
2. Every item in "evidence" must copy a key from FIELDS into "field" and copy its value EXACTLY into "value". Do not compute, round or invent numbers. If you want to argue from a comparison, cite both fields as separate evidence items and make the comparison in "why".
3. "would_be_wrong_if" must be a falsifiable condition stated in terms of one or more FIELDS keys (for example "indicators.last_close falls below indicators.sma_200").
4. Be specific and short. No hedging language, no disclaimers.
5. Output a single JSON object and nothing else, with exactly these keys:
{
  "stance": "favourable" | "unfavourable" | "neutral",
  "thesis": string (20-900 chars),
  "evidence": [ {"field": string, "value": number|string|boolean|null, "why": string}, ... ] (2 to 8 items),
  "confidence": number between 0 and 1,
  "would_be_wrong_if": string (10-400 chars),
  "horizon_days": integer between 5 and 365
}"""


def technical_prompt(stable: dict[str, Any], as_of: str) -> tuple[str, str, dict[str, Any]]:
    """Return (system, user, fields). `fields` is what evidence is checked against."""
    view = {
        "ticker": stable.get("ticker", {}),
        "instrument": {k: stable.get("instrument", {}).get(k) for k in ("description", "currency", "saxo_symbol")},
        "indicators": stable.get("indicators", {}),
        "bars_last_20": stable.get("bars", [])[-20:],
    }
    fields = flatten(view)
    user = (
        f"DATE: {as_of}\n"
        f"INSTRUMENT: {view['ticker'].get('symbol')} ({view['instrument'].get('description')})\n\n"
        "FIELDS (key: value). Cite keys verbatim.\n"
        + "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
        + "\n\nAnswer with the JSON object only."
    )
    return TECHNICAL_SYSTEM, user, fields


def run_view(cfg: Config, llm: LlmClient, con: sqlite3.Connection, run_id: str, ticker: str,
             stable: dict[str, Any], as_of: str, role_name: str = "technical_analyst",
             log=print) -> int | None:
    """Produce and store one analyst view. Returns the analyst_views id, or None if no
    valid view could be obtained within cfg.pipeline.max_attempts."""
    role = cfg.roles[role_name]
    system, user, fields = technical_prompt(stable, as_of)
    feedback = ""
    for attempt in range(1, cfg.pipeline.max_attempts + 1):
        user_msg = user if not feedback else user + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED:\n" + feedback + "\nFix every problem and answer again with the JSON object only."
        error: str | None = None
        resp = None
        try:
            resp = llm.complete(role, system, user_msg)
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
        if view is not None:
            vid = con.execute(
                "INSERT INTO analyst_views(run_id, ticker, role, thesis, evidence_json, confidence, would_be_wrong_if, "
                "llm_call_id, stance, horizon_days) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, ticker, role_name, view.thesis, j([e.model_dump() for e in view.evidence]), view.confidence,
                 view.would_be_wrong_if, call_id, view.stance, view.horizon_days),
            ).lastrowid
            con.commit()
            log(f"{ticker}: {role_name} {view.stance} (conf {view.confidence:.2f}, {len(view.evidence)} evidence, attempt {attempt})")
            return vid
        log(f"{ticker}: {role_name} attempt {attempt} rejected: {error[:160] if error else '?'}")
        feedback = error or ""
        if error and error.startswith("llm:"):
            break  # provider failure: retries already happened inside the client
    return None
