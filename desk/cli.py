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

from . import __version__, analysts, benchmark, config as cfgmod, datapack, guard
from .llm import LlmClient, OpenAICompatClient
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


async def cmd_guard(cfg: Config) -> None:
    guard.check_environment(cfg)
    async with McpClient(cfg.saxo_mcp, "saxo") as saxo:
        key = await guard.check_account(cfg, saxo)
    print(f"guard ok, SIM account {key}")


async def run_once(cfg: Config, con, *, saxo_inproc: Any | None = None, fmp_inproc: Any | None = None,
                   llm: LlmClient | None = None, today=None, log=print) -> str:
    """One full run: guard, data packs, analyst views, benchmark. Returns run_id;
    raises GuardFailure or the underlying error. A rejected analyst view does not
    fail the run; it shows up in `desk report` as a missing view."""
    if cfg.pipeline.analysts and llm is None:
        llm = OpenAICompatClient(cfg.providers)
    run_id = uuid.uuid4().hex[:12]
    con.execute(
        "INSERT INTO runs(run_id, started_at, status, environment, config_hash, git_commit, views_expected) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, now(), "running", cfg.environment.value, cfg.raw_hash, _git(),
         len(cfg.universe.tickers) * len(cfg.pipeline.analysts)),
    )
    con.commit()
    try:
        guard.check_environment(cfg)
        fmp_on = cfg.fmp_mcp.enabled and any(cfg.fmp_mcp.tools.values())
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
                    log(f"{t.symbol}: {len(pack['stable']['bars'])} bars, hash {pack['stable_hash'][:12]}")
                    as_of = pack["stable"]["indicators"].get("as_of") or str(today or "")
                    for role_name in cfg.pipeline.analysts:
                        analysts.run_view(cfg, llm, con, run_id, t.symbol, pack["stable"], as_of, role_name, log=log)
            finally:
                if fmp_cm:
                    await fmp_cm.__aexit__(None, None, None)
            b = await benchmark.snapshot(cfg, saxo, con, run_id, today=today)
            log(f"benchmark {b['symbol']}: {b['price']:.2f} {b['currency']}, value {b['value']:.2f}")
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


async def cmd_run(cfg: Config) -> None:
    con = connect(cfg.storage.db_path)
    run_id = await run_once(cfg, con)
    print(f"run {run_id} ok")


def cmd_report(cfg: Config) -> int:
    """Recent runs, benchmark, same-day hash check, analyst views. Returns 1 on a hash mismatch."""
    con = connect(cfg.storage.db_path)
    print("runs:")
    for r in con.execute("SELECT run_id, started_at, status, account_key, git_commit, error FROM runs ORDER BY started_at DESC LIMIT 10"):
        err = f"  {r['error'][:60]}" if r["error"] else ""
        print(f"  {r['run_id']}  {r['started_at']}  {r['status']:12s} {r['git_commit'] or '-':8s} {r['account_key'] or '-'}{err}")
    print("benchmark:")
    for r in con.execute("SELECT date, symbol, currency, price, value FROM benchmark_snapshots ORDER BY date DESC LIMIT 5"):
        print(f"  {r['date']}  {r['symbol']}  {r['price']:.2f} {r['currency'] or ''}  value {r['value']:.2f}")

    print("same-day data-pack hashes, successful runs with the same code and config only")
    print("(phase 0 exit criterion: one hash per ticker per day):")
    bad = 0
    for r in con.execute(
        "SELECT d.ticker, substr(d.created_at,1,10) AS day, r.config_hash, COALESCE(r.git_commit,'-') AS commit_, "
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
        "SELECT r.run_id, substr(r.started_at,1,10) AS day, r.views_expected, "
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
    return 1 if bad else 0


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
    sub.add_parser("guard", help="run the startup guard only")
    sub.add_parser("run", help="one full run: guard, data packs, benchmark snapshot")
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
        elif a.cmd == "guard":
            asyncio.run(cmd_guard(cfg))
        elif a.cmd == "run":
            asyncio.run(cmd_run(cfg))
        elif a.cmd == "report":
            sys.exit(cmd_report(cfg))
        elif a.cmd == "views":
            cmd_views(cfg, a.limit)
        elif a.cmd == "prompt":
            cmd_prompt(cfg, a.ticker)
    except guard.GuardFailure as e:
        print(f"GUARD FAILED: {e}", file=sys.stderr)
        sys.exit(2)
    except McpConnectError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
