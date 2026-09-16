"""Phase 2 end to end: fake Saxo + fake Gemini + fake Anthropic, decision forced."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from desk import cli, ledger
from desk.db import connect
from desk.llm import AnthropicClient, OpenAICompatClient, Router
from desk.schemas import TraderProposal, check_proposal
from tests.conftest import make_saxo
from tests.test_analyst import FakeGemini, fields_for, good_view

D0 = date(2026, 9, 16)


class FakeAnthropic:
    """Looks like anthropic.Anthropic().messages.parse for our purposes."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=ans)], stop_reason="end_turn",
                               model=kwargs["model"], usage=SimpleNamespace(input_tokens=3000, output_tokens=400),
                               stop_details=None)


def proposal(action="long", weight=0.10, stop=("indicators.sma_200", "<"), fields=None, **over):
    p = {"action": action, "target_weight": weight,
         "winning_argument": "Technical timing is acceptable and nothing in the book argues against it.",
         "rejected_arguments": [{"role": "technical_analyst", "argument": "momentum fading", "why_rejected": "not in evidence"}],
         "sided_with": ["technical_analyst"], "stop": None, "horizon_days": 90, "confidence": 0.6}
    if isinstance(stop, dict):
        p["stop"] = stop
    elif stop and fields is not None:
        p["stop"] = {"field": stop[0], "op": stop[1], "value": fields[stop[0]] * (0.97 if stop[1] == "<" else 1.03)}
    p.update(over)
    return json.dumps(p)


def make_router(cfg, monkeypatch, gemini_answers, anthropic_answers):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    fa = FakeAnthropic(anthropic_answers)
    router = Router(cfg.providers, clients={
        "openai_compat": OpenAICompatClient(cfg.providers, transport=FakeGemini(gemini_answers), retries=0, backoff_s=0),
        "anthropic": AnthropicClient(cfg.providers, client_factory=lambda prov: fa),
    })
    return router, fa


async def warmup(cfg, con):
    """A phase-0 style run so the fields (and their values) are known to the test."""
    saved = cfg.pipeline.analysts, cfg.pipeline.trader
    cfg.pipeline.analysts, cfg.pipeline.trader = [], None
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), today=D0, log=lambda *_: None)
    cfg.pipeline.analysts, cfg.pipeline.trader = saved
    return fields_for(con)


async def test_decision_day_produces_chain_and_next_run_fills(cfg, monkeypatch, capsys):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    router, fa = make_router(cfg, monkeypatch, [good_view(f), good_view(f)], [proposal(fields=f)])

    r1 = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), llm=router, today=D0, decide=True, log=lambda *_: None)
    d = con.execute("SELECT * FROM decisions WHERE run_id=?", (r1,)).fetchone()
    assert d["action"] == "long" and d["status"] == "pending" and d["final_weight"] == 0.10 and d["source"] == "trader"
    v = con.execute("SELECT * FROM risk_verdicts WHERE id=?", (d["verdict_id"],)).fetchone()
    assert v["verdict"] == "pass" and "MAX_POSITION_PCT" in json.loads(v["rules_checked_json"])
    p = con.execute("SELECT * FROM trader_proposals WHERE id=?", (d["proposal_id"],)).fetchone()
    assert json.loads(p["sided_with_json"]) == ["technical_analyst"] and json.loads(p["stop_json"])["field"] == "indicators.sma_200"
    assert con.execute("SELECT decided FROM runs WHERE run_id=?", (r1,)).fetchone()["decided"] == 1
    # the trader saw the book, the risk limits, the analyst view and the fields, with no sampling params
    sent = fa.calls[0]
    assert sent["model"] == "claude-sonnet-5" and sent["output_format"] is TraderProposal
    assert sent["output_config"] == {"effort": "medium"} and "temperature" not in sent
    user = sent["messages"][0]["content"]
    assert "RISK LIMITS" in user and "MAX_POSITION_PCT limit 0.15" in user and "[technical_analyst] stance=favourable" in user
    assert "no position in MSFT" in user and "indicators.sma_200:" in user
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0          # nothing fills on decision day

    # next day: fill at that day's mark, snapshot the book, no new decision (Thursday)
    d1 = date(2026, 9, 17)
    r2 = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=d1), llm=router, today=d1, log=lambda *_: None)
    fill = con.execute("SELECT * FROM fills").fetchone()
    assert fill["ticker"] == "MSFT" and fill["quantity"] > 0 and fill["run_id"] == r2
    snap = con.execute("SELECT * FROM portfolio_snapshots WHERE date=?", (d1.isoformat(),)).fetchone()
    assert json.loads(snap["positions_json"])["MSFT"]["weight"] == pytest.approx(0.10, abs=0.001)
    assert con.execute("SELECT decided FROM runs WHERE run_id=?", (r2,)).fetchone()["decided"] == 0
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1

    cli.cmd_decisions(cfg, 5); cli.cmd_book(cfg); assert cli.cmd_report(cfg) == 0
    out = capsys.readouterr().out
    assert "decision 1  MSFT  long" in out and "risk: pass" in out and "fill: qty" in out
    assert "trader sided with: technical_analyst 1/1" in out and "benchmark: return" in out


async def test_stop_triggers_exit_next_day(cfg, monkeypatch):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    # a stop just below today's close: tomorrow's fake bars drift, so make it certain to trigger
    stop_val = f["indicators.last_close"] * 1.5
    router, _ = make_router(cfg, monkeypatch, [good_view(f)] * 8,
                            [proposal(fields=f, stop={"field": "indicators.last_close", "op": "<", "value": stop_val})])
    # the trader's stop is already breached today -> rejected; second attempt fine
    router.clients["anthropic"]._clients = {}
    fa = FakeAnthropic([proposal(fields=f, stop={"field": "indicators.last_close", "op": "<", "value": stop_val}),
                        proposal(fields=f, stop={"field": "indicators.last_close", "op": "<", "value": f["indicators.last_close"] * 0.5})])
    router.clients["anthropic"].client_factory = lambda prov: fa
    r1 = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), llm=router, today=D0, decide=True, log=lambda *_: None)
    errs = [r["error"] for r in con.execute("SELECT error FROM llm_calls WHERE role='trader' ORDER BY attempt")]
    assert errs[0].startswith("proposal:") and "already triggered" in errs[0] and errs[1] is None

    d1 = date(2026, 9, 17)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=d1), llm=router, today=d1, log=lambda *_: None)   # fills
    assert ledger.active_stop(con, "MSFT")["value"] == pytest.approx(f["indicators.last_close"] * 0.5)
    # force the stop: rewrite it above the price, then run the monitor
    con.execute("UPDATE trader_proposals SET stop_json=? WHERE ticker='MSFT' AND source='trader'",
                (json.dumps({"field": "indicators.last_close", "op": "<", "value": stop_val}),))
    con.commit()
    d2 = date(2026, 9, 18)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=d2), llm=router, today=d2, log=lambda *_: None)
    ex = con.execute("SELECT * FROM decisions WHERE action='exit'").fetchone()
    assert ex is not None and ex["source"] == "stop" and ex["status"] == "pending"
    prop = con.execute("SELECT * FROM trader_proposals WHERE id=?", (ex["proposal_id"],)).fetchone()
    assert prop["source"] == "stop" and prop["winning_argument"].startswith("stop fired") and prop["llm_call_id"] is None
    d3 = date(2026, 9, 19)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=d3), llm=router, today=d3, log=lambda *_: None)
    assert ledger.positions(con) == {}
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


async def test_time_stop_and_no_duplicate_exit(cfg, monkeypatch):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    router, _ = make_router(cfg, monkeypatch, [good_view(f)] * 4, [proposal(fields=f)])
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), llm=router, today=D0, decide=True, log=lambda *_: None)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=date(2026, 9, 17)), llm=router, today=date(2026, 9, 17), log=lambda *_: None)
    con.execute("UPDATE fills SET filled_at='2026-01-01T00:00:00Z'")          # pretend it was opened long ago
    con.commit()
    late = date(2026, 9, 18)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=late), llm=router, today=late, log=lambda *_: None)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=late), llm=router, today=late, log=lambda *_: None)
    exits = con.execute("SELECT source, status FROM decisions WHERE action='exit'").fetchall()
    assert len(exits) == 1 and exits[0]["source"] == "time_stop"


async def test_veto_records_verdict_and_no_pending_decision(cfg, monkeypatch):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    router, _ = make_router(cfg, monkeypatch, [good_view(f)], [proposal(weight=0.40, fields=f)])
    r1 = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), llm=router, today=D0, decide=True, log=lambda *_: None)
    v = con.execute("SELECT * FROM risk_verdicts").fetchone()
    assert v["verdict"] == "resize" and v["rule_fired"] == "MAX_POSITION_PCT" and v["adjusted_weight"] == 0.15
    d = con.execute("SELECT * FROM decisions").fetchone()
    assert d["final_weight"] == 0.15 and d["status"] == "pending"


def test_cadence(cfg):
    con = connect(cfg.storage.db_path)
    assert cli.should_decide(cfg, con, date(2026, 9, 14)) is True        # Monday
    assert cli.should_decide(cfg, con, date(2026, 9, 16)) is False       # Wednesday
    assert cli.should_decide(cfg, con, date(2026, 9, 16), force=True) is True
    cfg.pipeline.decision.weekdays = []
    cfg.pipeline.decision.min_days_between = 7
    con.execute("INSERT INTO runs(run_id, started_at, status, environment, config_hash, decided) VALUES ('x','2026-09-14T22:30:00','ok','SIM','h',1)")
    assert cli.should_decide(cfg, con, date(2026, 9, 16)) is False
    assert cli.should_decide(cfg, con, date(2026, 9, 21)) is True
    cfg.pipeline.trader = None
    assert cli.should_decide(cfg, con, date(2026, 9, 21)) is False


def test_check_proposal_rules():
    fields = {"indicators.last_close": 100.0, "indicators.sma_200": 90.0, "instrument.currency": "USD"}
    ok = TraderProposal.model_validate(json.loads(proposal(fields=fields)))
    assert check_proposal(ok, fields, False, ["technical_analyst"]) == []
    bad = TraderProposal.model_validate(json.loads(proposal(action="hold", fields=fields, sided_with=["ghost"],
                                                            stop={"field": "instrument.currency", "op": "<", "value": 1})))
    probs = check_proposal(bad, fields, False, ["technical_analyst"])
    assert any("no open position" in p for p in probs) and any("not numeric" in p for p in probs) and any("ghost" in p for p in probs)
    none = TraderProposal.model_validate(json.loads(proposal(action="none", weight=0.0, stop=None)))
    assert check_proposal(none, fields, False, ["technical_analyst"]) == []
