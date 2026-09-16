"""desk: scheduled batch job. Runs, writes to SQLite, exits. Never a server."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import uuid
from typing import Any

from . import benchmark, config as cfgmod, datapack, guard
from .config import Config
from .db import connect, j, now
from .mcp_client import McpClient, McpConnectError


def _git() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


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
                   today=None, log=print) -> str:
    """One full phase-0 run. Returns run_id; raises GuardFailure or the underlying error."""
    run_id = uuid.uuid4().hex[:12]
    con.execute(
        "INSERT INTO runs(run_id, started_at, status, environment, config_hash, git_commit) VALUES (?,?,?,?,?,?)",
        (run_id, now(), "running", cfg.environment.value, cfg.raw_hash, _git()),
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
    """Print recent runs, benchmark rows and the same-day hash check. Returns 1 if any day disagrees."""
    con = connect(cfg.storage.db_path)
    print("runs:")
    for r in con.execute("SELECT run_id, started_at, status, account_key, error FROM runs ORDER BY started_at DESC LIMIT 10"):
        err = f"  {r['error'][:60]}" if r["error"] else ""
        print(f"  {r['run_id']}  {r['started_at']}  {r['status']}  {r['account_key'] or '-'}{err}")
    print("benchmark:")
    for r in con.execute("SELECT date, symbol, currency, price, value FROM benchmark_snapshots ORDER BY date DESC LIMIT 5"):
        print(f"  {r['date']}  {r['symbol']}  {r['price']:.2f} {r['currency'] or ''}  value {r['value']:.2f}")
    print("same-day data-pack hashes (phase 0 exit criterion: one hash per ticker per day):")
    bad = 0
    for r in con.execute(
        "SELECT ticker, substr(created_at,1,10) AS day, COUNT(*) AS runs, COUNT(DISTINCT stable_hash) AS hashes "
        "FROM data_packs GROUP BY ticker, day ORDER BY day DESC, ticker LIMIT 20"
    ):
        flag = "" if r["hashes"] == 1 else "  <-- MISMATCH"
        bad += r["hashes"] != 1
        print(f"  {r['day']}  {r['ticker']:8s} runs={r['runs']} distinct_hashes={r['hashes']}{flag}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="desk")
    p.add_argument("--config", default="config/desk.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover-tools", help="list the tools an MCP server exposes")
    d.add_argument("server", choices=["saxo", "fmp"])
    sub.add_parser("guard", help="run the startup guard only")
    sub.add_parser("run", help="one full run: guard, data packs, benchmark snapshot")
    sub.add_parser("report", help="recent runs, benchmark, same-day hash check")
    a = p.parse_args(argv)
    cfg = cfgmod.load(a.config)
    try:
        if a.cmd == "discover-tools":
            asyncio.run(cmd_discover(cfg, a.server))
        elif a.cmd == "guard":
            asyncio.run(cmd_guard(cfg))
        elif a.cmd == "run":
            asyncio.run(cmd_run(cfg))
        elif a.cmd == "report":
            sys.exit(cmd_report(cfg))
    except guard.GuardFailure as e:
        print(f"GUARD FAILED: {e}", file=sys.stderr)
        sys.exit(2)
    except McpConnectError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
