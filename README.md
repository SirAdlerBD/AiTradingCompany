# desk — phase 2

A multi-agent trading research desk that runs against a Saxo **SIM** account
only. A scheduled batch job (systemd timer) starts, passes a startup guard,
freezes one hashed data pack per ticker, asks each configured analyst for a
view, fills yesterday's decisions in a shadow ledger, checks stops, and on a
decision day asks the trader, runs the risk rules in code, and logs a
decision. Nothing is ever sent to the broker. See `PLAN.md` for the phases.

The roster and everything about cadence live in `config/desk.yaml`:

- **analysts**: `technical_analyst` and `fundamentals_analyst` (both OpenAI
  gpt-5.6-luna; Gemini's free tier proved unreliable), each with one question
  and evidence validated against the pack. Providers are per role in config; any
  OpenAI-compatible endpoint works, with per-provider request quirks
  (`max_tokens_param`, `send_temperature`).
- **trader** (Claude): weighs the views, names the winning and rejected
  arguments, proposes an action, a target weight and a stop that code can check.
- **risk**: not a model. `config/risk_rules.yaml` limits evaluated in
  `desk/risk.py`; every verdict cites a rule id and the numbers it saw.
- **gate and ledger**: the only writer of decisions; fills happen at the next
  run's quote, so there is no look-ahead.

Data is fetched by the orchestrator (code, not a model):
[saxo-mcp](https://github.com/SirAdlerBD/saxo-mcp) for quotes, bars and
account state, and Financial Modeling Prep's REST API for fundamentals (its
hosted MCP server needs OAuth, so the code calls the REST API it wraps).
Analysts only ever see the frozen data pack; they never fetch.

## Layout

```
config/desk.yaml        everything a run needs, nothing it must not have
config/risk_rules.yaml  deterministic risk rules (evaluated by code from phase 2)
desk/config.py          pydantic config; Environment enum has exactly one member: SIM
desk/mcp_client.py      thin client over mcp.Client (streamable HTTP or in-process)
desk/guard.py           startup guard (env, account allowlist, trading hard block)
desk/datapack.py        instrument resolution, bars, quote, indicators, stable hash
desk/fmp.py             FMP REST client; fetches reduced to `keep` fields; `desk fmp-check`
desk/indicators.py      deterministic technical indicators computed in code from the bars
desk/schemas.py         AnalystView (pydantic) and the evidence check against the pack
desk/llm.py             one call signature per role; OpenAI-compatible HTTP provider (Gemini)
desk/analysts.py        analyst step: prompt, call, validate, feed rejections back once, store
desk/trader.py          trader prompt, TraderProposal validation, synthetic exit proposals
desk/risk.py            rule engine over config/risk_rules.yaml; drawdown hysteresis; correlation
desk/ledger.py          shadow book from fills; pending decisions filled at the next mark; snapshots
desk/benchmark.py       start capital bought into the index ETF on day 0, marked daily
desk/db.py              SQLite schema plus column migrations; every table carries run_id
desk/performance.py     shadow book vs benchmark from fills + frozen quotes; table and dated chart
desk/cli.py             desk run [--decide|--no-decide] | performance | report | views | decisions | book | prompt | guard | fmp-check | discover-*
PLAN.md                 phases, exit criteria, status
deploy/                 systemd unit + timer, install script, env template
tests/                  fake saxo-mcp in process, same tool names and payload shapes
```

## Bring-up on the VPS

saxo-mcp's HTTP server must already be running under pm2 on
`127.0.0.1:3000` in SIM with `SAXO_TRADING` left disabled.

1. `sudo ./deploy/install.sh` from the repo root. Creates the `desk` system
   user, `/opt/desk`, `/var/lib/desk`, a venv, and enables the timer.
2. Fill `/etc/desk/desk.env`: the saxo-mcp `MCP_ACCESS_TOKEN` as
   `SAXO_SIM_MCP_TOKEN`, every SIM `AccountKey` from `get_account_summary`
   as `SAXO_SIM_ACCOUNT_KEYS`, a Google AI Studio key as `GEMINI_API_KEY`,
   and an Anthropic key as `ANTHROPIC_API_KEY`.
3. `desk guard` must print `guard ok, SIM account <key>`.
4. `desk run` twice on the same day, then `desk report`. The report exits 1 if
   any ticker has more than one data-pack hash on one day.
5. Optional, for fundamentals: put `FMP_API_KEY` in the env file, run
   `desk fmp-check MSFT`, fix any path or field it flags, set
   `fmp_rest.enabled: true` and add `fundamentals_analyst` to
   `pipeline.analysts`. Until then the pack carries `fundamentals: {}`. A
   section FMP refuses for a symbol (HTTP 402, outside the subscription tier)
   is left out of that symbol's pack, listed under `fundamentals._unavailable`,
   named in the run's warnings, and the analyst is told it is unavailable.

**After every `git pull`, rerun `sudo ./deploy/install.sh`.** The service runs
the copy in `/opt/desk`, not your clone; the script rsyncs it and records the
deployed commit in `/opt/desk/COMMIT`. Every `desk` command prints the version,
commit and config path it is using on its first line, so a stale copy is
visible at a glance.

`desk run --verbose` (or `-v`) prints, in addition to the compact lines the
timer logs: every analyst view in full (thesis, each evidence item with its
field, value and reasoning, the wrong-if condition), the trader's reasoning
(which views it weighed, what it sided with, the winning and rejected
arguments, the stop), every risk check with the numbers it saw even when
nothing fired, and per-call token counts with estimated cost, then a run
total. Nothing recorded in SQLite changes with the flag.

Running by hand as the service user:

```
sudo systemctl start desk.service && journalctl -u desk -n 30
```

## How an analyst view is produced

1. The data pack's `ticker`, `instrument`, `indicators` and last 20 bars are
   flattened to `key: value` lines (about 150 citable fields, ~2k tokens).
2. The analyst gets that block and a fixed system prompt (`desk/analysts.py`).
   The technical analyst's one question: is entry timing acceptable this week,
   and what would make that wrong? It has no tools and no outside knowledge.
3. The answer must be one JSON object matching `AnalystView`: stance, thesis,
   2-8 evidence items `{field, value, why}`, confidence, `would_be_wrong_if`,
   horizon. Every evidence `field` must be a key from the block and `value`
   must equal the pack's value (numbers within 0.5%).
4. A rejected answer is sent back once with the exact problems. Every attempt
   is stored in `llm_calls` with its error; only a valid view reaches
   `analyst_views`. A missing view never fails the run; `desk report` shows it.

`desk prompt MSFT` prints the exact prompt from the latest stored pack without
calling a model, for prompt work. `desk views` prints recent views with their
evidence.

Models are pinned to explicit versions (see the comment in `config/desk.yaml`).
When a provider retires one, the run keeps going, `llm_calls.error` holds the
404, and `desk report` prints it under the missing view. `desk discover-models
gemini` lists what the provider serves and exits 1 if a pinned model is gone.

Cost: one Gemini Flash call per ticker per run, about 2k tokens in and 400
out, well under a cent. The free tier covers it; its terms allow prompt use for
training, which is acceptable for public SIM data.

## How a decision is made (phase 2)

Every run, after the packs and views: pending decisions from the previous run
fill at today's mark (fee from config), each open position's stop and the
time stop are evaluated, and exits are queued through the gate. On a decision
day (`pipeline.decision`: a weekday list and a minimum gap, or `desk run
--decide`), per ticker: the trader gets FIELDS, the analyst views, the book and
the risk limits, and answers with a `TraderProposal` (action, target weight,
winning and rejected arguments, which analysts it sided with, a stop as
`{field, op, value}`). Code validates it (a stop must cite a numeric field and
not be breached today), the risk rules pass, resize or veto it, and the gate
writes a pending decision. `desk decisions` prints the whole chain per
decision: proposal, verdict with numbers, decision, fill.

The trader runs on Claude through the Anthropic SDK with structured output.
Effort and model are per role in config; no sampling parameters are sent.

## Did it beat the index: `desk performance`

The one number the project is judged on. Recomputed every time from `fills`
and the quotes frozen in each day's data pack (no live call, reproducible),
put next to `benchmark_snapshots` rebased to the same start, which is the day
of the first decision by default (`--since` to change). Per day: shadow value,
benchmark value, both cumulative returns, the DELTA in percentage points, and
a running ahead/behind tally. Below `--min-days` (default 10 trading days) it
says TOO EARLY and skips the chart; nothing is ever annualised. The chart is a
dated PNG under `storage.reports_dir` when matplotlib is installed (`pip
install -e '.[charts]'`, done by `deploy/install.sh`), else a dependency-free
SVG. A mismatch between the recomputed value and the stored snapshot is
printed as a warning.

## Phase 2 exit criteria

- Any decision is reconstructible from the DB alone (`desk decisions`).
- The shadow book is marked daily against the benchmark for two weeks
  (`desk report` shows both, plus turnover, verdict counts, holding period and
  which analyst the trader sided with).

## Phase 1 exit criteria

- Five consecutive daily runs each store a valid view per ticker: `desk
  report` shows `views 1/1` with no `MISSING VIEW`, and rejected attempts
  trending to zero. Persistent rejections mean the prompt needs work before
  anything else is added.
- Read the views (`desk views --limit 5`): evidence should be the fields a
  human would pick, and `would_be_wrong_if` should be checkable next week.

## Phase 0 exit criteria (met)

- Two same-day runs produce identical `stable_hash` per ticker. `desk report`
  compares successful runs with the same config hash and commit only, since a
  code or config change legitimately changes the pack (the pack embeds the
  ticker record and the indicator set).
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
- Gemini's OpenAI-compatible endpoint (`/v1beta/openai/chat/completions`, bearer
  key, `response_format: json_object`, `usage.prompt_tokens`). The tests use a
  fake with that wire shape; the first real `desk run` confirms it.
- FMP REST path names (`profile`, `key-metrics-ttm`, `ratios-ttm`,
  `financial-growth`, `price-target-consensus`). The row shapes were read
  from FMP's own MCP tools, which wrap these paths; `desk fmp-check` confirms
  the names and the `keep` fields in one call.
- The first real trader call: the Anthropic SDK's `messages.parse` with
  `output_format=TraderProposal` and `output_config.effort`.
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
