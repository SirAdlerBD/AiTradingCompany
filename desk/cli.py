"""desk: scheduled batch job. Runs, writes to SQLite, exits. Never a server."""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from . import __version__, analysts, benchmark, config as cfgmod, datapack, guard, ledger, performance, risk, trader
from .llm import LlmClient, LlmError, Router, list_models
from .schemas import Stop, flatten
from datetime import date as _date
from .config import Config
from .db import connect, j, now
from .mcp_client import McpClient, McpConnectError


PKG_DIR = Path(__file__).resolve().parent


def _git() -> str | None:
    """Commit of the code that is running. /opt/desk has no .git (rsync excludes it),
    so deploy/install.sh writes the commit to a COMMIT file next to the package."""
    marker = PKG_DIR.parent / "COMMIT"
    if marker.exists():
        return marker.read_text().strip() or None
    try:
        return subprocess.check_output(["git", "-C", str(PKG_DIR), "rev-parse", "--short", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def banner(config_path: str) -> str:
    """One line that says which code and which config are running. Printed by every
    command so a stale deployed copy is visible instead of a confusing error."""
    return f"desk {__version__} commit {_git() or 'unknown'} from {PKG_DIR}, config {Path(config_path).resolve()}"


async def cmd_discover(cfg: Config, which: str) -> None:
    server = cfg.saxo_mcp if which == "saxo" else cfg.fmp_mcp
    async with McpClient(server, which) as c:
        for t in await c.list_tools():
            print(f"{t['name']:32s} args={','.join(t['args'])}")
            print(f"{'':32s} {t['description'][:110]}")


def cmd_discover_models(cfg: Config, provider: str) -> None:
    prov = cfg.providers.get(provider)
    if prov is None:
        sys.exit(f"unknown provider {provider!r}; configured: {sorted(cfg.providers)}")
    pinned = {r.model for r in cfg.roles.values() if r.provider == provider}
    names = list_models(prov)
    for n in names:
        mark = "  <-- pinned in config" if n in pinned else ""
        print(f"{n}{mark}")
    missing = sorted(pinned - set(names))
    if missing:
        print(f"\nWARNING: pinned model(s) not served by {provider}: {missing}", file=sys.stderr)
        sys.exit(1)
    print(f"\n{len(names)} models; every pinned model is served.")


async def cmd_guard(cfg: Config) -> None:
    guard.check_environment(cfg)
    async with McpClient(cfg.saxo_mcp, "saxo") as saxo:
        key = await guard.check_account(cfg, saxo)
    print(f"guard ok, SIM account {key}")


def should_decide(cfg: Config, con, today, force: bool | None = None) -> bool:
    """Decision cadence from config: weekday list and a minimum gap since the last decision run."""
    if force is not None:
        return force
    if not cfg.pipeline.trader:
        return False
    cad = cfg.pipeline.decision
    if cad.weekdays and cfgmod.WEEKDAYS[today.weekday()] not in cad.weekdays:
        return False
    if cad.min_days_between:
        last = con.execute("SELECT MAX(COALESCE(run_date, substr(started_at,1,10))) FROM runs WHERE decided=1 AND status='ok'").fetchone()[0]
        if last and (today - _date.fromisoformat(last)).days < cad.min_days_between:
            return False
    return True


def gate(cfg: Config, con, run_id: str, ticker: str, proposal_id: int, verdict: risk.Verdict, source: str, log=print) -> int:
    """The only writer of decisions. Logs the verdict and the resulting decision; never executes."""
    p = con.execute("SELECT action FROM trader_proposals WHERE id=?", (proposal_id,)).fetchone()
    vid = con.execute(
        "INSERT INTO risk_verdicts(proposal_id, rules_checked_json, rule_fired, verdict, original_weight, adjusted_weight, "
        "numbers_json, run_id, ticker, risk_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (proposal_id, j(verdict.rules_checked), verdict.rule_fired, verdict.verdict, verdict.original_weight,
         verdict.adjusted_weight, j(verdict.numbers), run_id, ticker, None, now()),
    ).lastrowid
    action = p["action"]
    if verdict.verdict == "veto":
        action = "hold" if action == "hold" else "none"
    status = "noop" if action in ("none",) else "pending"
    did = con.execute(
        "INSERT INTO decisions(run_id, ticker, proposal_id, verdict_id, action, final_weight, created_at, status, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, ticker, proposal_id, vid, action, verdict.adjusted_weight, now(), status, source),
    ).lastrowid
    con.commit()
    log(f"{ticker}: risk {verdict.verdict}" + (f" ({verdict.rule_fired})" if verdict.rule_fired else "")
        + f" -> decision {did} {action} w={verdict.adjusted_weight:.3f} [{status}]")
    return did


def monitor(cfg: Config, con, run_id: str, rules: risk.RuleSet, packs: dict[str, dict], book: dict, today, log=print) -> None:
    """Daily: evaluate each open position's stop and the time stop; queue exits through the gate."""
    time_stop = risk.time_stop_days(rules)
    for ticker, pos in book["positions"].items():
        if con.execute("SELECT 1 FROM decisions WHERE ticker=? AND action='exit' AND status='pending'", (ticker,)).fetchone():
            continue
        stable = packs.get(ticker)
        fields = flatten(analysts.pack_view(stable, ["indicators", "fundamentals"])) if stable else {}
        stop = ledger.active_stop(con, ticker)
        reason = None
        source = None
        if stop and fields:
            st = Stop.model_validate(stop)
            hit = st.triggered(fields)
            if hit:
                reason, source = f"stop fired: {st.field} {st.op} {st.value} (value {fields.get(st.field)})", "stop"
        held = (today - _date.fromisoformat(pos["opened_at"])).days
        if reason is None and time_stop is not None and held > time_stop:
            reason, source = f"time stop: held {held} days > MAX_HOLDING_DAYS {time_stop}", "time_stop"
        if reason:
            pid = trader.synthetic_proposal(con, run_id, ticker, source, reason)
            v = risk.evaluate(rules, "exit", ticker, 0.0, ledger.context(cfg, con, rules, {}, today, {}))
            gate(cfg, con, run_id, ticker, pid, v, source, log=log)
            log(f"{ticker}: {reason}")


def _sector(stable: dict | None) -> str | None:
    if not stable:
        return None
    prof = (stable.get("fundamentals") or {}).get("profile") or {}
    return prof.get("sector") if isinstance(prof, dict) else None


async def run_once(cfg: Config, con, *, saxo_inproc: Any | None = None, fmp_inproc: Any | None = None,
                   llm: LlmClient | None = None, today=None, decide: bool | None = None, log=print) -> str:
    """One full run, in stages:
      1. guard            4. fill pending decisions at today's mark (shadow ledger)
      2. data packs       5. monitor open positions: stops, time stop
      3. analyst views    6. on a decision day: trader -> risk -> gate per ticker
                          7. portfolio and benchmark snapshots
    Returns run_id; raises GuardFailure or the underlying error. A rejected model
    answer never fails the run; it shows up in `desk report`."""
    today = today or _date.today()
    rules = risk.load_rules(cfg.path(cfg.risk.rules_file)) if cfg.pipeline.trader else None
    run_id = uuid.uuid4().hex[:12]
    con.execute(
        "INSERT INTO runs(run_id, started_at, status, environment, config_hash, git_commit, views_expected, risk_version, run_date) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, now(), "running", cfg.environment.value, cfg.raw_hash, _git(), 0, rules.version if rules else None,
         today.isoformat()),
    )
    con.commit()
    try:
        guard.check_environment(cfg)
        deciding = should_decide(cfg, con, today, decide)
        run_analysts = bool(cfg.pipeline.analysts) and (cfg.pipeline.analysts_every_run or deciding)
        if run_analysts or deciding:
            llm = llm or Router(cfg.providers)
        con.execute("UPDATE runs SET views_expected=?, decided=? WHERE run_id=?",
                    (len(cfg.universe.tickers) * len(cfg.pipeline.analysts) if run_analysts else 0, int(deciding), run_id))
        fmp_on = cfg.fmp_mcp.enabled and bool(cfg.fmp_mcp.fetch)
        packs: dict[str, dict] = {}
        async with McpClient(cfg.saxo_mcp, "saxo", inproc=saxo_inproc) as saxo:
            key = await guard.check_account(cfg, saxo)
            con.execute("UPDATE runs SET account_key=? WHERE run_id=?", (key, run_id))
            con.commit()
            fmp_cm = McpClient(cfg.fmp_mcp, "fmp", inproc=fmp_inproc) if fmp_on else None
            fmp = await fmp_cm.__aenter__() if fmp_cm else None
            try:
                for t in cfg.universe.tickers:
                    pack = await datapack.build(cfg, saxo, fmp, con, t, today=today)
                    con.execute(
                        "INSERT INTO data_packs(run_id, ticker, stable_json, stable_hash, volatile_json, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (run_id, t.symbol, j(pack["stable"]), pack["stable_hash"], j(pack["volatile"]), now()),
                    )
                    con.executemany(
                        "INSERT OR REPLACE INTO price_history VALUES (?,?,?,?,?,?,?)",
                        [(t.symbol, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                         for b in pack["stable"]["bars"]],
                    )
                    con.commit()
                    packs[t.symbol] = pack["stable"]
                    log(f"{t.symbol}: {len(pack['stable']['bars'])} bars, hash {pack['stable_hash'][:12]}")
                    if run_analysts:
                        as_of = pack["stable"]["indicators"].get("as_of") or today.isoformat()
                        for role_name in cfg.pipeline.analysts:
                            analysts.run_view(cfg, llm, con, run_id, t.symbol, pack["stable"], as_of, role_name, log=log)
            finally:
                if fmp_cm:
                    await fmp_cm.__aexit__(None, None, None)
            b = await benchmark.snapshot(cfg, saxo, con, run_id, today=today)
            log(f"benchmark {b['symbol']}: {b['price']:.2f} {b['currency']}, value {b['value']:.2f}")

        if cfg.pipeline.trader:
            prices = ledger.mark_prices(con, run_id, cfg.ledger.mark)
            ledger.fill_pending(cfg, con, run_id, prices, today, log=log)
            book = ledger.book(cfg, con, prices)
            monitor(cfg, con, run_id, rules, packs, book, today, log=log)
            if deciding:
                sectors = {t: _sector(packs.get(t)) for t in packs}
                for t in cfg.universe.tickers:
                    stable = packs.get(t.symbol)
                    if not stable:
                        continue
                    if con.execute("SELECT 1 FROM decisions WHERE ticker=? AND status='pending'", (t.symbol,)).fetchone():
                        log(f"{t.symbol}: skipping trader, a decision is already pending")
                        continue
                    book = ledger.book(cfg, con, prices)
                    as_of = stable["indicators"].get("as_of") or today.isoformat()
                    pid = trader.propose(cfg, llm, con, run_id, t.symbol, stable, as_of, book, rules, log=log)
                    if pid is None:
                        continue
                    prop = con.execute("SELECT action, target_weight FROM trader_proposals WHERE id=?", (pid,)).fetchone()
                    ctx = ledger.context(cfg, con, rules, prices, today, sectors)
                    v = risk.evaluate(rules, prop["action"], t.symbol, float(prop["target_weight"]), ctx, sectors.get(t.symbol))
                    gate(cfg, con, run_id, t.symbol, pid, v, "trader", log=log)
            snap = ledger.snapshot(cfg, con, run_id, prices, today)
            log(f"book: value {snap['total_value']:.2f}, cash {snap['cash']:.2f}, drawdown {snap['drawdown']:+.2%}, "
                f"{len(snap['positions'])} position(s)")
        con.execute("UPDATE runs SET status='ok', finished_at=? WHERE run_id=?", (now(), run_id))
        con.commit()
        return run_id
    except guard.GuardFailure as e:
        con.execute("UPDATE runs SET status='guard_failed', finished_at=?, error=? WHERE run_id=?",
                    (now(), str(e), run_id))
        con.commit()
        raise
    except Exception as e:
        con.execute("UPDATE runs SET status='failed', finished_at=?, error=? WHERE run_id=?",
                    (now(), repr(e), run_id))
        con.commit()
        raise


async def cmd_run(cfg: Config, decide: bool | None = None) -> None:
    con = connect(cfg.storage.db_path)
    run_id = await run_once(cfg, con, decide=decide)
    print(f"run {run_id} ok")


def cmd_report(cfg: Config) -> int:
    """Recent runs, benchmark, same-day hash check, analyst views. Returns 1 on a hash mismatch."""
    con = connect(cfg.storage.db_path)
    print("runs:")
    for r in con.execute("SELECT run_id, started_at, status, account_key, git_commit, error, decided FROM runs ORDER BY started_at DESC LIMIT 10"):
        err = f"  {r['error'][:60]}" if r["error"] else ""
        dec = "  decided" if r["decided"] else ""
        print(f"  {r['run_id']}  {r['started_at']}  {r['status']:12s} {r['git_commit'] or '-':8s} {r['account_key'] or '-'}{dec}{err}")
    print("benchmark:")
    for r in con.execute("SELECT date, symbol, currency, price, value FROM benchmark_snapshots ORDER BY date DESC LIMIT 5"):
        print(f"  {r['date']}  {r['symbol']}  {r['price']:.2f} {r['currency'] or ''}  value {r['value']:.2f}")

    print("same-day data-pack hashes, successful runs with the same code and config only")
    print("(phase 0 exit criterion: one hash per ticker per day):")
    bad = 0
    for r in con.execute(
        "SELECT d.ticker, COALESCE(r.run_date, substr(d.created_at,1,10)) AS day, r.config_hash, COALESCE(r.git_commit,'-') AS commit_, "
        "COUNT(*) AS runs, COUNT(DISTINCT d.stable_hash) AS hashes "
        "FROM data_packs d JOIN runs r USING(run_id) WHERE r.status='ok' "
        "GROUP BY d.ticker, day, r.config_hash, commit_ ORDER BY day DESC, d.ticker LIMIT 20"
    ):
        flag = "" if r["hashes"] == 1 else "  <-- MISMATCH"
        bad += r["hashes"] != 1
        print(f"  {r['day']}  {r['ticker']:8s} cfg {r['config_hash'][:8]} code {r['commit_']:8s} runs={r['runs']} distinct_hashes={r['hashes']}{flag}")
    skipped = con.execute(
        "SELECT COUNT(DISTINCT d.run_id) FROM data_packs d JOIN runs r USING(run_id) WHERE r.status!='ok'"
    ).fetchone()[0]
    if skipped:
        print(f"  ({skipped} non-ok run(s) with data packs excluded)")

    rows = con.execute(
        "SELECT r.run_id, COALESCE(r.run_date, substr(r.started_at,1,10)) AS day, r.views_expected, "
        "(SELECT COUNT(*) FROM analyst_views v WHERE v.run_id=r.run_id) AS views, "
        "(SELECT COUNT(*) FROM llm_calls c WHERE c.run_id=r.run_id AND c.error IS NOT NULL) AS rejected, "
        "(SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls c WHERE c.run_id=r.run_id) AS cost "
        "FROM runs r WHERE r.status='ok' AND COALESCE(r.views_expected,0) > 0 ORDER BY r.started_at DESC LIMIT 10"
    ).fetchall()
    if rows:
        print("analyst views, last 10 successful runs with analysts configured")
        print("(phase 1 exit criterion: five consecutive days with every view present):")
        for r in rows:
            flag = "" if r["views"] == r["views_expected"] else "  <-- MISSING VIEW"
            print(f"  {r['day']}  {r['run_id']}  views {r['views']}/{r['views_expected']}  "
                  f"rejected attempts {r['rejected']}  cost ${r['cost']:.4f}{flag}")
            if flag:
                last = con.execute(
                    "SELECT ticker, role, error FROM llm_calls WHERE run_id=? AND error IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (r["run_id"],),
                ).fetchone()
                if last:
                    print(f"      last error ({last['ticker']}, {last['role']}): {last['error'][:200]}")
    if cfg.pipeline.trader:
        print_kpis(cfg, con)
    return 1 if bad else 0


def print_kpis(cfg: Config, con) -> None:
    """Discipline KPIs from the tables. Alpha is checked last and least."""
    snaps = con.execute("SELECT date, total_value, drawdown FROM portfolio_snapshots ORDER BY date").fetchall()
    bench = con.execute("SELECT date, value FROM benchmark_snapshots ORDER BY date").fetchall()
    if not snaps:
        print("book: no snapshots yet")
        return
    first, last = snaps[0], snaps[-1]
    start = cfg.benchmark.start_capital
    ret = last["total_value"] / start - 1
    mdd = min(float(r["drawdown"] or 0) for r in snaps)
    bret = (bench[-1]["value"] / start - 1) if bench else None
    bvals = [float(r["value"]) for r in bench]
    bmdd = 0.0
    peak = 0.0
    for v in bvals:
        peak = max(peak, v)
        bmdd = min(bmdd, v / peak - 1 if peak else 0.0)
    days = (_date.fromisoformat(last["date"]) - _date.fromisoformat(first["date"])).days or 1
    traded = con.execute("SELECT COALESCE(SUM(ABS(value)),0) FROM fills").fetchone()[0]
    avg_val = sum(float(r["total_value"]) for r in snaps) / len(snaps)
    turnover_annual = (traded / avg_val) * 365 / days if avg_val else 0.0
    verdicts = con.execute("SELECT verdict, COUNT(*) AS n FROM risk_verdicts GROUP BY verdict").fetchall()
    vc = {r["verdict"]: r["n"] for r in verdicts}
    decisions = con.execute("SELECT source, action, COUNT(*) AS n FROM decisions GROUP BY source, action").fetchall()
    closed = con.execute(
        "SELECT f.ticker, MIN(f.filled_at) AS o, MAX(f.filled_at) AS c FROM fills f WHERE f.ticker IN "
        "(SELECT ticker FROM fills GROUP BY ticker HAVING ABS(SUM(quantity)) < 1e-9) GROUP BY f.ticker").fetchall()
    holds = [(_date.fromisoformat(r["c"][:10]) - _date.fromisoformat(r["o"][:10])).days for r in closed]
    print(f"book since {first['date']} ({days} days): value {last['total_value']:.2f}, return {ret:+.2%}, max drawdown {mdd:+.2%}")
    if bret is not None:
        print(f"benchmark: return {bret:+.2%}, max drawdown {bmdd:+.2%}; excess {ret - bret:+.2%}")
    print(f"discipline: turnover {turnover_annual:.2f}x/yr, verdicts pass={vc.get('pass',0)} resize={vc.get('resize',0)} veto={vc.get('veto',0)}, "
          f"closed positions {len(closed)} avg hold {sum(holds)/len(holds):.0f}d" if holds else
          f"discipline: turnover {turnover_annual:.2f}x/yr, verdicts pass={vc.get('pass',0)} resize={vc.get('resize',0)} veto={vc.get('veto',0)}, no closed positions yet")
    if decisions:
        print("decisions: " + ", ".join(f"{r['source']}/{r['action']}={r['n']}" for r in decisions))
    props = con.execute("SELECT sided_with_json FROM trader_proposals WHERE source='trader' AND sided_with_json IS NOT NULL").fetchall()
    if props:
        counts: dict[str, int] = {}
        for r in props:
            for role in json.loads(r["sided_with_json"]):
                counts[role] = counts.get(role, 0) + 1
        print("trader sided with: " + ", ".join(f"{k} {v}/{len(props)}" for k, v in sorted(counts.items())) or "(no analyst named)")


def cmd_book(cfg: Config) -> None:
    con = connect(cfg.storage.db_path)
    r = con.execute("SELECT * FROM portfolio_snapshots ORDER BY date DESC LIMIT 1").fetchone()
    if not r:
        print("no snapshot yet")
        return
    print(f"{r['date']}  total {r['total_value']:.2f}  cash {r['cash']:.2f}  drawdown {float(r['drawdown'] or 0):+.2%}")
    for t, p in sorted(json.loads(r["positions_json"]).items()):
        stop = ledger.active_stop(con, t)
        print(f"  {t:8s} qty {p['quantity']:.4f} @ {p['price']:.2f}  value {p['value']:.2f}  w {p['weight']:.3f}  "
              f"pnl {p['unrealized_pct']:+.1%}  since {p['opened_at']}  stop {stop['field'] + stop['op'] + str(stop['value']) if stop else '-'}")
    for d in con.execute("SELECT id, ticker, action, final_weight, source FROM decisions WHERE status='pending'"):
        print(f"  pending: decision {d['id']} {d['ticker']} {d['action']} w={d['final_weight']:.3f} ({d['source']})")


def cmd_decisions(cfg: Config, limit: int) -> None:
    """The full chain per decision: proposal, verdict, decision, fill."""
    con = connect(cfg.storage.db_path)
    rows = con.execute(
        "SELECT d.id, d.created_at, d.ticker, d.action, d.final_weight, d.status, d.source, "
        "p.action AS p_action, p.target_weight, p.winning_argument, p.rejected_json, p.sided_with_json, p.stop_condition, "
        "v.verdict, v.rule_fired, v.numbers_json, "
        "(SELECT quantity FROM fills f WHERE f.decision_id=d.id) AS qty, (SELECT price FROM fills f WHERE f.decision_id=d.id) AS px "
        "FROM decisions d JOIN trader_proposals p ON p.id=d.proposal_id JOIN risk_verdicts v ON v.id=d.verdict_id "
        "ORDER BY d.id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("no decisions yet")
    for r in rows:
        print(f"== {r['created_at'][:10]}  decision {r['id']}  {r['ticker']}  {r['action']} w={r['final_weight']:.3f}  [{r['status']}] source {r['source']}")
        print(f"   proposal: {r['p_action']} w={r['target_weight']:.3f}  sided_with {r['sided_with_json']}  stop {r['stop_condition'] or '-'}")
        print(f"   why: {r['winning_argument']}")
        for rej in json.loads(r["rejected_json"] or "[]"):
            print(f"   rejected [{rej.get('role')}]: {rej.get('argument')} -> {rej.get('why_rejected')}")
        print(f"   risk: {r['verdict']}" + (f" ({r['rule_fired']})" if r["rule_fired"] else "") + f"  numbers {r['numbers_json']}")
        if r["qty"] is not None:
            print(f"   fill: qty {r['qty']:.4f} @ {r['px']:.2f}")


def cmd_performance(cfg: Config, min_days: int, since: str | None, chart: str, force_chart: bool) -> int:
    """Shadow book vs benchmark, recomputed from fills and frozen pack quotes. Exit 0 always; the
    verdict line and the TOO EARLY banner carry the meaning."""
    con = connect(cfg.storage.db_path)
    s = performance.compute(cfg, con, since)
    print(performance.render_table(s, min_days))
    if not s.rows:
        return 0
    if s.trading_days < min_days and not force_chart:
        print(f"chart skipped: fewer than {min_days} trading days (pass --chart-anyway to draw it regardless)")
        return 0
    if chart != "none":
        p = performance.render_chart(cfg, s, backend=chart)
        print(f"chart: {p}")
    return 0


def cmd_views(cfg: Config, limit: int) -> None:
    """Print the latest analyst views with their evidence, newest first."""
    con = connect(cfg.storage.db_path)
    rows = con.execute(
        "SELECT v.*, r.started_at FROM analyst_views v JOIN runs r USING(run_id) ORDER BY v.id DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("no views yet")
    for v in rows:
        print(f"== {v['started_at'][:10]}  {v['ticker']}  {v['role']}  {v['stance']}  conf {v['confidence']:.2f}  "
              f"horizon {v['horizon_days']}d  run {v['run_id']}")
        print(f"   thesis: {v['thesis']}")
        for e in json.loads(v["evidence_json"]):
            print(f"   - {e['field']} = {e['value']}: {e['why']}")
        print(f"   wrong if: {v['would_be_wrong_if']}")


def cmd_prompt(cfg: Config, ticker: str) -> None:
    """Print the exact analyst prompt built from the latest stored data pack. No model call."""
    con = connect(cfg.storage.db_path)
    row = con.execute(
        "SELECT d.stable_json FROM data_packs d JOIN runs r USING(run_id) WHERE d.ticker=? AND r.status='ok' "
        "ORDER BY d.created_at DESC LIMIT 1", (ticker,)
    ).fetchone()
    if not row:
        sys.exit(f"no successful data pack for {ticker}; run `desk run` first")
    stable = json.loads(row["stable_json"])
    system, user, fields = analysts.technical_prompt(stable, stable.get("indicators", {}).get("as_of", "?"))
    print("### SYSTEM\n" + system + "\n\n### USER\n" + user)
    print(f"\n### {len(fields)} citable fields, ~{(len(system) + len(user)) // 4} tokens")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="desk")
    p.add_argument("--config", default="config/desk.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover-tools", help="list the tools an MCP server exposes")
    d.add_argument("server", choices=["saxo", "fmp"])
    dm = sub.add_parser("discover-models", help="list the models a provider serves; exit 1 if a pinned model is missing")
    dm.add_argument("provider")
    sub.add_parser("guard", help="run the startup guard only")
    r = sub.add_parser("run", help="one full run: guard, packs, views, ledger, (decision), snapshots")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--decide", action="store_true", help="run the trader today regardless of the cadence")
    g.add_argument("--no-decide", action="store_true", help="skip the trader today regardless of the cadence")
    sub.add_parser("book", help="current shadow book and pending decisions")
    pf = sub.add_parser("performance", help="shadow book vs benchmark, day by day, plus a dated chart")
    pf.add_argument("--min-days", type=int, default=10, help="trading days needed before the verdict is shown as meaningful")
    pf.add_argument("--since", help="comparison start date YYYY-MM-DD (default: the first decision)")
    pf.add_argument("--chart", choices=["auto", "png", "svg", "none"], default="auto",
                    help="png needs matplotlib (pip install -e '.[charts]'); svg needs nothing; auto prefers png")
    pf.add_argument("--chart-anyway", action="store_true", help="draw the chart even below --min-days")
    dc = sub.add_parser("decisions", help="decision chains: proposal, verdict, decision, fill")
    dc.add_argument("--limit", type=int, default=10)
    sub.add_parser("report", help="recent runs, benchmark, same-day hash check, analyst views")
    v = sub.add_parser("views", help="print the latest analyst views with evidence")
    v.add_argument("--limit", type=int, default=5)
    pr = sub.add_parser("prompt", help="print the analyst prompt for a ticker from the latest data pack (no model call)")
    pr.add_argument("ticker")
    a = p.parse_args(argv)
    print(banner(a.config))
    try:
        cfg = cfgmod.load(a.config)
    except Exception as e:
        print(f"CONFIG ERROR in {a.config}: {e}", file=sys.stderr)
        print("If the error names a field this version does not use, the deployed copy is stale: "
              "rerun deploy/install.sh from the repo you pulled.", file=sys.stderr)
        sys.exit(4)
    try:
        if a.cmd == "discover-tools":
            asyncio.run(cmd_discover(cfg, a.server))
        elif a.cmd == "discover-models":
            cmd_discover_models(cfg, a.provider)
        elif a.cmd == "guard":
            asyncio.run(cmd_guard(cfg))
        elif a.cmd == "run":
            asyncio.run(cmd_run(cfg, decide=True if a.decide else False if a.no_decide else None))
        elif a.cmd == "book":
            cmd_book(cfg)
        elif a.cmd == "performance":
            sys.exit(cmd_performance(cfg, a.min_days, a.since, a.chart, a.chart_anyway))
        elif a.cmd == "decisions":
            cmd_decisions(cfg, a.limit)
        elif a.cmd == "report":
            sys.exit(cmd_report(cfg))
        elif a.cmd == "views":
            cmd_views(cfg, a.limit)
        elif a.cmd == "prompt":
            cmd_prompt(cfg, a.ticker)
    except guard.GuardFailure as e:
        print(f"GUARD FAILED: {e}", file=sys.stderr)
        sys.exit(2)
    except (McpConnectError, LlmError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
