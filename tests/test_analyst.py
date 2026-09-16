"""Phase 1 end to end: fake saxo-mcp + fake Gemini (OpenAI-compatible wire format)."""
import json
from datetime import date

import httpx

from desk import cli
from desk.db import connect
from desk.llm import OpenAICompatClient
from tests.conftest import make_saxo

TODAY = date(2026, 9, 16)


class FakeGemini:
    """Answers scripted per attempt; records every request body."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "body": json})
        ans = self.answers.pop(0)
        if isinstance(ans, int):
            return httpx.Response(ans, text="upstream trouble", request=httpx.Request("POST", url))
        payload = {"id": "x", "model": "gemini-3.7-flash", "choices": [{"message": {"role": "assistant", "content": ans}}],
                   "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))


def good_view(fields):
    return json.dumps({
        "stance": "favourable",
        "thesis": "Price is above the 200-day average and the 50-day sits above the 200-day; entry timing acceptable.",
        "evidence": [
            {"field": "indicators.last_close", "value": fields["indicators.last_close"], "why": "current level"},
            {"field": "indicators.sma_200", "value": fields["indicators.sma_200"], "why": "long-term trend anchor"},
            {"field": "indicators.rsi_14", "value": fields["indicators.rsi_14"], "why": "not overbought"},
        ],
        "confidence": 0.62,
        "would_be_wrong_if": "indicators.last_close closes below indicators.sma_200 for five sessions",
        "horizon_days": 90,
    })


def fields_for(con, ticker="MSFT"):
    from desk.analysts import technical_prompt
    stable = json.loads(con.execute("SELECT stable_json FROM data_packs WHERE ticker=? ORDER BY created_at DESC LIMIT 1",
                                    (ticker,)).fetchone()["stable_json"])
    return technical_prompt(stable, stable["indicators"]["as_of"])[2]


def make_llm(cfg, monkeypatch, answers):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = FakeGemini(answers)
    return OpenAICompatClient(cfg.providers, transport=fake, retries=1, backoff_s=0), fake


async def test_valid_view_is_stored_with_evidence(cfg, monkeypatch):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    # first a phase-0 style run to learn the real field values, then the analyst run
    cfg.pipeline.analysts = []
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), today=TODAY, log=lambda *_: None)
    f = fields_for(con)
    assert f["indicators.sma_200"] is not None and f["indicators.bars_available"] == 259

    cfg.pipeline.analysts = ["technical_analyst"]
    llm, fake = make_llm(cfg, monkeypatch, [good_view(f)])
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), llm=llm, today=TODAY, log=lambda *_: None)

    v = con.execute("SELECT * FROM analyst_views WHERE run_id=?", (run_id,)).fetchone()
    assert v["role"] == "technical_analyst" and v["stance"] == "favourable" and v["horizon_days"] == 90
    ev = json.loads(v["evidence_json"])
    assert [e["field"] for e in ev] == ["indicators.last_close", "indicators.sma_200", "indicators.rsi_14"]
    c = con.execute("SELECT * FROM llm_calls WHERE id=?", (v["llm_call_id"],)).fetchone()
    assert c["error"] is None and c["attempt"] == 1 and c["tokens_in"] == 1200
    assert abs(c["cost_usd"] - (1200 * 0.30 + 300 * 2.50) / 1e6) < 1e-9
    assert "FIELDS" in c["prompt"] and "indicators.sma_200:" in c["prompt"]
    # wire format: bearer key, json mode, system+user messages
    req = fake.requests[0]
    assert req["url"].endswith("/v1beta/openai/chat/completions")
    assert req["headers"]["Authorization"] == "Bearer k"
    assert req["body"]["response_format"] == {"type": "json_object"} and req["body"]["model"] == "gemini-3.7-flash"
    assert [m["role"] for m in req["body"]["messages"]] == ["system", "user"]
    assert cli.cmd_report(cfg) == 0


async def test_hallucinated_number_rejected_then_corrected(cfg, monkeypatch, capsys):
    con = connect(cfg.storage.db_path)
    cfg.pipeline.analysts = []
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), today=TODAY, log=lambda *_: None)
    f = fields_for(con)
    bad = json.loads(good_view(f))
    bad["evidence"][0]["value"] = f["indicators.last_close"] * 1.2      # invented number
    bad["evidence"][1]["field"] = "indicators.macd"                      # invented field

    cfg.pipeline.analysts = ["technical_analyst"]
    llm, fake = make_llm(cfg, monkeypatch, [json.dumps(bad), good_view(f)])
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), llm=llm, today=TODAY, log=lambda *_: None)

    calls = con.execute("SELECT attempt, error FROM llm_calls WHERE run_id=? ORDER BY attempt", (run_id,)).fetchall()
    assert [c["attempt"] for c in calls] == [1, 2]
    assert calls[0]["error"].startswith("evidence:") and "does not match" in calls[0]["error"] and "macd" in calls[0]["error"]
    assert calls[1]["error"] is None
    # the retry carried the rejection reasons back to the model
    assert "PREVIOUS ANSWER WAS REJECTED" in fake.requests[1]["body"]["messages"][1]["content"]
    assert con.execute("SELECT COUNT(*) FROM analyst_views WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    # the phase-0 style run recorded 0 expected views, so the report does not flag it
    cli.cmd_report(cfg)
    out = capsys.readouterr().out
    assert "MISSING VIEW" not in out and f"{run_id}  views 1/1" in out


async def test_persistent_garbage_leaves_no_view_but_run_ok(cfg, monkeypatch, capsys):
    con = connect(cfg.storage.db_path)
    cfg.pipeline.analysts = ["technical_analyst"]
    llm, _ = make_llm(cfg, monkeypatch, ["not json at all", "```json\n{\"stance\": \"bullish\"}\n```"])
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), llm=llm, today=TODAY, log=lambda *_: None)
    assert con.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()["status"] == "ok"
    assert con.execute("SELECT COUNT(*) FROM analyst_views").fetchone()[0] == 0
    errs = [r["error"] for r in con.execute("SELECT error FROM llm_calls ORDER BY attempt")]
    assert errs[0].startswith("schema:") and errs[1].startswith("schema:")
    cli.cmd_report(cfg)
    assert "MISSING VIEW" in capsys.readouterr().out


async def test_provider_5xx_is_retried_then_recorded(cfg, monkeypatch):
    con = connect(cfg.storage.db_path)
    cfg.pipeline.analysts = ["technical_analyst"]
    llm, fake = make_llm(cfg, monkeypatch, [503, 503])
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), llm=llm, today=TODAY, log=lambda *_: None)
    assert len(fake.requests) == 2
    rows = con.execute("SELECT error, response FROM llm_calls WHERE run_id=?", (run_id,)).fetchall()
    assert len(rows) == 1 and rows[0]["error"].startswith("llm:") and "503" in rows[0]["error"]


def test_migration_adds_columns_to_phase0_db(cfg):
    import sqlite3
    con = sqlite3.connect(cfg.storage.db_path)
    con.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, ticker TEXT, role TEXT NOT NULL, "
                "model TEXT NOT NULL, prompt_hash TEXT NOT NULL, prompt TEXT NOT NULL, response TEXT NOT NULL, "
                "tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER, cost_usd REAL, created_at TEXT NOT NULL)")
    con.commit(); con.close()
    con = connect(cfg.storage.db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)")}
    assert {"attempt", "error"} <= cols
    cols = {r[1] for r in con.execute("PRAGMA table_info(analyst_views)")}
    assert {"stance", "horizon_days"} <= cols
    connect(cfg.storage.db_path)   # idempotent


async def test_retired_model_404_is_recorded_and_reported(cfg, monkeypatch, capsys):
    con = connect(cfg.storage.db_path)
    cfg.pipeline.analysts = ["technical_analyst"]
    llm, fake = make_llm(cfg, monkeypatch, [404])
    run_id = await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=TODAY), llm=llm, today=TODAY, log=lambda *_: None)
    assert len(fake.requests) == 1                       # 404 is not retried
    err = con.execute("SELECT error FROM llm_calls WHERE run_id=?", (run_id,)).fetchone()["error"]
    assert "404" in err
    cli.cmd_report(cfg)
    out = capsys.readouterr().out
    assert "MISSING VIEW" in out and "last error (MSFT, technical_analyst): llm:" in out and "404" in out


def test_discover_models_flags_missing_pin(cfg, monkeypatch, capsys):
    from desk.llm import list_models
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return httpx.Response(200, json={"data": [{"id": "models/gemini-3.7-flash"}, {"id": "models/gemini-3.7-pro"}]},
                              request=httpx.Request("GET", url))

    names = list_models(cfg.providers["gemini"], get=fake_get)
    assert names == ["gemini-3.7-flash", "gemini-3.7-pro"]
    assert seen["url"].endswith("/v1beta/openai/models") and seen["auth"] == "Bearer k"
