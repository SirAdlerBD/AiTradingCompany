# desk — phase 0

A multi-agent trading research desk that runs against a Saxo **SIM** account
only. Phase 0 is the plumbing: a scheduled batch job (systemd timer) that
starts, passes a startup guard, freezes one hashed data pack per ticker,
snapshots the benchmark, writes SQLite, and exits. No LLM calls yet.

Data comes from two MCP servers that the orchestrator (code, not a model)
calls: [saxo-mcp](https://github.com/SirAdlerBD/saxo-mcp) for quotes, bars
and account state, and the FMP MCP server for fundamentals (phase 1+).
Analysts will only ever see the frozen data pack; they never fetch.

## Layout

```
config/desk.yaml        everything a run needs, nothing it must not have
config/risk_rules.yaml  deterministic risk rules (evaluated by code from phase 2)
desk/config.py          pydantic config; Environment enum has exactly one member: SIM
desk/mcp_client.py      thin client over mcp.Client (streamable HTTP or in-process)
desk/guard.py           startup guard (env, account allowlist, trading hard block)
desk/datapack.py        instrument resolution, bars, quote, stable hash
desk/benchmark.py       start capital bought into the index ETF on day 0, marked daily
desk/db.py              SQLite schema; every table carries run_id
desk/cli.py             desk discover-tools | guard | run | report
deploy/                 systemd unit + timer, install script, env template
tests/                  fake saxo-mcp in process, same tool names and payload shapes
```

## Bring-up on the VPS

saxo-mcp's HTTP server must already be running under pm2 on
`127.0.0.1:3000` in SIM with `SAXO_TRADING` left disabled.

1. `sudo ./deploy/install.sh` from the repo root. Creates the `desk` system
   user, `/opt/desk`, `/var/lib/desk`, a venv, and enables the timer.
2. Fill `/etc/desk/desk.env`: the saxo-mcp `MCP_ACCESS_TOKEN` as
   `SAXO_SIM_MCP_TOKEN`, and every SIM `AccountKey` from `get_account_summary`
   as `SAXO_SIM_ACCOUNT_KEYS`.
3. `desk guard` must print `guard ok, SIM account <key>`.
4. `desk run` twice on the same day, then `desk report`. The report exits 1 if
   any ticker has more than one data-pack hash on one day.
5. Optional, for fundamentals: `desk discover-tools fmp`, fill in
   `fmp_mcp.tools` and set `fmp_mcp.enabled: true`. Until then the pack carries
   `fundamentals: {}`.

**After every `git pull`, rerun `sudo ./deploy/install.sh`.** The service runs
the copy in `/opt/desk`, not your clone; the script rsyncs it and records the
deployed commit in `/opt/desk/COMMIT`. Every `desk` command prints the version,
commit and config path it is using on its first line, so a stale copy is
visible at a glance.

Running by hand as the service user:

```
sudo systemctl start desk.service && journalctl -u desk -n 30
```

## Phase 0 exit criteria

- Two same-day runs produce identical `stable_hash` per ticker (`desk report`
  checks this; `tests/test_run.py` proves it against the fake server).
- `desk guard` fails when any `SAXO_*LIVE*` variable is set, when the MCP
  reports an account key outside the allowlist, or when the MCP reports
  trading enabled.
- `benchmark_snapshots` has one row per trading day for five days.

## What is verified vs. still assumed

Verified against saxo-mcp's source (`src/tools/marketdata.ts`,
`src/tools/portfolio.ts`): tool names, argument names (`keywords`,
`assetTypes`, `exchangeId`, `uic`, `assetType`, `horizon`, `count`), the
`instruments[]`, `bars[]` and `accounts[]` envelopes, and the `trading` string
in the account summary. The tests use exactly these shapes.

Verified on the SIM account itself: `SXR8:xetr` (UIC 1095726, EUR) and
`MSFT:xnas` (UIC 261, USD) resolve. Saxo's `ExchangeId` is an internal code
that differs by asset type on one venue (Xetra stocks `FSE`, Xetra ETFs
`XETR_ETF`) and a wrong value as a search filter silently returns nothing, so
listings are identified by symbol + MIC (the two halves of Saxo's `Symbol`)
and matched client-side.

Still assumed, check on first real run:
- The FMP MCP URL, auth style and tool names (`fmp_mcp` is disabled by default).
- Saxo's infoprice `Quote.Mid` is present for stocks; if not, the code derives
  mid from bid/ask, then falls back to `LastTraded`.

## Where LIVE cannot get in

- `Environment` enum has one member; `runs.environment` has a CHECK constraint.
- No LIVE URL, token or account key exists in config or env; the guard aborts
  if a `SAXO_*LIVE*` variable is present (that covers saxo-mcp's own
  `SAXO_ALLOW_LIVE` switch).
- Startup asserts every account key the MCP reports is on the SIM allowlist,
  and that the MCP's trading hard block is still on.
- The `desk` user cannot read pm2's home or the MCP servers' env files.
- Nothing imports or constructs an order payload. Phase 5 adds that behind a
  second config file that does not exist by default.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/desk --config config/desk.yaml report      # needs a writable storage.db_path
```
