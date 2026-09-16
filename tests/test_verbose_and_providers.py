"""--verbose output and per-provider request quirks (OpenAI vs Gemini)."""
import json
from datetime import date

from desk import cli
from desk.db import connect
from desk.llm import OpenAICompatClient
from desk.log import Log
from tests.conftest import make_saxo
from tests.test_analyst import FakeGemini, good_view
from tests.test_fundamentals import make_fmp
from tests.test_trader_flow import make_router, proposal, warmup

D0 = date(2026, 9, 16)


def test_openai_request_uses_max_completion_tokens_and_role_prices(cfg, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    fake = FakeGemini([good_view({"indicators.last_close": 1, "indicators.sma_200": 1, "indicators.rsi_14": 1})])
    client = OpenAICompatClient(cfg.providers, transport=fake, retries=0, backoff_s=0)
    role = cfg.roles["technical_analyst"]
    assert role.provider == "openai" and role.model == "gpt-5.6-luna"
    resp = client.complete(role, "sys", "user")
    body = fake.requests[0]["body"]
    assert body["max_completion_tokens"] == 1500 and "max_tokens" not in body and body["temperature"] == 0.2
    assert fake.requests[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer k"
    assert abs(resp.cost_usd - (1200 * 1.00 + 300 * 6.00) / 1e6) < 1e-9        # role prices, not Gemini's

    cfg.providers["openai"].send_temperature = False
    client.complete(role, "sys", "user") if fake.answers else None
    fake.answers.append(good_view({"indicators.last_close": 1, "indicators.sma_200": 1, "indicators.rsi_14": 1}))
    client.complete(role, "sys", "user")
    assert "temperature" not in fake.requests[-1]["body"]

    # Gemini keeps the classic names
    fund = cfg.roles["fundamentals_analyst"]
    assert fund.provider == "gemini"
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    fake.answers.append(good_view({"indicators.last_close": 1, "indicators.sma_200": 1, "indicators.rsi_14": 1}))
    client.complete(fund, "sys", "user")
    assert fake.requests[-1]["body"]["max_tokens"] == 1500 and fake.requests[-1]["url"].startswith("https://generativelanguage")


async def test_verbose_prints_views_reasoning_risk_and_usage(cfg, monkeypatch):
    cfg.universe.history_days = 260
    con = connect(cfg.storage.db_path)
    f = await warmup(cfg, con)
    router, _ = make_router(cfg, monkeypatch, [good_view(f)], [proposal(fields=f)])

    quiet_lines, verbose_lines = [], []
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), llm=router, today=D0, decide=True,
                       log=Log(verbose=False, out=quiet_lines.append))
    router, _ = make_router(cfg, monkeypatch, [good_view(f)], [proposal(fields=f)])
    con2 = connect(cfg.storage.db_path.with_name("v.sqlite"))
    cfg2 = cfg.model_copy(deep=True); cfg2.storage.db_path = cfg.storage.db_path.with_name("v.sqlite")
    await warmup(cfg2, con2)
    await cli.run_once(cfg2, con2, saxo_inproc=make_saxo(today=D0), llm=router, today=D0, decide=True,
                       log=Log(verbose=True, out=verbose_lines.append))
    q, v = "\n".join(quiet_lines), "\n".join(verbose_lines)

    # compact output unchanged: one line per step, no blocks, no usage lines, but the run total
    assert "MSFT: technical_analyst favourable (conf 0.62, 3 evidence, attempt 1)" in q
    assert "┌─" not in q and "[usage]" not in q and "model calls: 2" in q
    # verbose: full view
    assert "┌─ technical_analyst on MSFT: favourable, confidence 0.62, horizon 90d" in v
    assert "thesis: Price is above the 200-day average" in v
    assert f"- indicators.sma_200 = {f['indicators.sma_200']}: long-term trend anchor" in v
    assert "wrong if: indicators.last_close closes below indicators.sma_200" in v
    # verbose: trader reasoning
    assert "┌─ trader on MSFT: long target weight 0.100" in v and "views weighed: technical_analyst=favourable" in v
    assert "winning argument: Technical timing is acceptable" in v
    assert "rejected [technical_analyst]: momentum fading -> not in evidence" in v
    assert "stop: indicators.sma_200 <" in v
    # verbose: risk block even on pass, with the rules and numbers
    assert "┌─ risk on MSFT: PASS, no rule fired" in v and "rules checked: LONG_ONLY, PORTFOLIO_DD_HALT, MAX_POSITION_PCT" in v
    assert "MAX_SECTOR_PCT = not evaluated: sector unknown" in v and "cash_room_for_ticker = 0.9" in v
    # verbose: usage per call tagged by role, and the total
    assert "[usage] technical_analyst (gpt-5.6-luna) attempt 1 ok: in=1200 out=300 cost $0.0030" in v
    assert "[usage] trader (claude-sonnet-5) attempt 1 ok: in=3000 out=400 cost $0.0100" in v
    assert "model calls: 2, tokens in 4200 / out 700, estimated cost $0.0130" in v
    # and the database is identical in shape either way
    for c in (con, con2):
        assert c.execute("SELECT COUNT(*) FROM analyst_views").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


async def test_verbose_shows_rejected_answer_text(cfg, monkeypatch):
    cfg.pipeline.trader = None
    con = connect(cfg.storage.db_path)
    lines = []
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    client = OpenAICompatClient(cfg.providers, transport=FakeGemini(["garbage answer", "still garbage"]), retries=0, backoff_s=0)
    await cli.run_once(cfg, con, saxo_inproc=make_saxo(today=D0), llm=client, today=D0, log=Log(verbose=True, out=lines.append))
    out = "\n".join(lines)
    assert "rejected answer (technical_analyst, attempt 1): garbage answer" in out
    assert "[usage] technical_analyst (gpt-5.6-luna) attempt 1 rejected" in out
