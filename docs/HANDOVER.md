# desk: handover for the observation period

Written 2026-09-17, at the start of an 8 to 12 week unattended run. This is the
whole project in one place: what it is, how it works, why it is built the way
it is, what has actually been verified, what has not, and what to do next.
Where something is uncertain it says so.

## 1. What this is

A research desk that runs as a batch job on your VPS against a Saxo **SIM**
account. Every weekday after the US close it freezes a small, hashed set of
facts per ticker, asks two cheap model roles for a view, and on Mondays asks a
trader role for a decision. Code, not a model, then checks that decision
against written risk rules and records the outcome as a pending shadow trade.
The next day's run fills that trade at the quote it sees and marks the book.
Nothing is ever sent to the broker.

The question the project exists to answer is narrow: **does a disciplined,
rule-bound process beat holding the index?** The benchmark is SXR8, the EUR
S&P 500 ETF, bought once with the same starting capital and marked daily.
`desk performance` is the scoreboard.

What it can realistically achieve, honestly:

- It will not beat the index because of information. Every input is public
  and delayed. Two cheap analysts reading the same numbers as everyone else do
  not know anything the market does not.
- If it beats the index at all, it will be through discipline: position size
  limits, a cash floor, stops that code enforces, a turnover cap, a time stop,
  and no ability to chase. Those are measurable, and the report measures them
  directly: veto and resize counts, turnover, holding period, drawdown.
- Alpha is a lottery ticket you check once a quarter. With two tickers the
  performance number will be noise for months. The ahead/behind tally is a
  better thing to watch than the delta.
- The most valuable output may be the decision log itself: a chain per
  decision of what the analysts argued, what the trader chose and rejected,
  what the rules did, and what happened. That is something you can learn from
  even if the P&L is flat.

## 2. Architecture: one run, in the order data flows

One run is `desk run`, started by the systemd timer at 22:30 Europe/Brussels
on weekdays, or by hand. Stages, each writing to SQLite as it goes:

1. **Guard** (`desk/guard.py`). Refuses to run unless: the config environment
   is SIM (the only value that exists), no `SAXO_*LIVE*` variable is in the
   process environment, every account key the Saxo MCP reports is on your
   allowlist, and the MCP reports its trading hard block still on. Fails
   closed and records `guard_failed`.
2. **Data pack** (`desk/datapack.py`, `desk/indicators.py`, `desk/fmp.py`).
   Per ticker: instrument resolved once by symbol and MIC and cached; 250 daily
   bars from Saxo with today's partial bar dropped; deterministic indicators
   computed in code (moving averages, RSI, ATR, returns, 52-week range,
   realised volatility, drawdown, volume ratio); fundamentals from FMP's REST
   API reduced to slow-moving fields. All of that is the **stable** pack and
   is hashed. The live quote is stored separately as **volatile** so two runs
   on the same day hash identically. The pack is the only thing any model ever
   sees. Models never fetch.
3. **Analyst views** (`desk/analysts.py`, `desk/schemas.py`). Each configured
   analyst gets the flattened pack as `key: value` lines and a fixed prompt
   with one question. Technical: is entry timing acceptable now? Fundamentals:
   is this business worth owning at today's valuation over 6 to 12 months? The
   answer must be one JSON object: stance, thesis, 2 to 8 evidence items,
   confidence, a falsifiable wrong-if condition, horizon. Every evidence item
   must name a real field of the pack and quote its value within 0.5%. A wrong
   number or invented field is rejected and sent back once with the reasons.
   Every attempt is stored; only valid views reach `analyst_views`.
4. **Fills** (`desk/ledger.py`). Pending decisions from the previous run fill
   at today's mark (the quote mid from the pack) with the configured fee. A
   decision made after Monday's close therefore fills at Tuesday's close: no
   look-ahead.
5. **Monitor** (`cli.monitor`). For every open position: the stop attached to
   its decision is evaluated against today's pack, and the time stop from the
   rules. A hit becomes an exit decision through the gate, with a
   code-generated proposal so the chain is complete.
6. **Decision** (`desk/trader.py`, `desk/risk.py`, `cli.gate`), on decision
   days only (Monday by default; `--decide` forces one). Per ticker the trader
   gets the pack, the latest view from each analyst, the current book, the
   last decision on this name and the risk limits it will be held to. It must
   name the winning argument, the rejected ones, which analysts it sided with,
   an action, a target weight, and a stop as `{field, op, value}` that code can
   evaluate daily. Structural checks reject a stop on a non-numeric field or
   one already breached today. The risk engine then passes, resizes or vetoes
   against the YAML rules and records the numbers it saw. The gate is the only
   writer of decisions and never executes anything.
7. **Snapshots**. Portfolio value, cash, positions, peak and drawdown for the
   day; benchmark value for the day.

Reading tools: `desk report` (runs, hash check, views per run, discipline
KPIs, warnings), `desk views`, `desk decisions` (the full chain per decision),
`desk book`, `desk performance` (shadow book vs benchmark, recomputed from
fills and frozen quotes, with a dated chart), `desk config` (the merged
effective config), `desk prompt TICKER` (the exact analyst prompt, no call),
`desk fmp-check TICKER`, `desk discover-models PROVIDER`.

Config: `config/desk.yaml` is repo-managed; `config/local.yaml` is yours,
gitignored, deep-merged over it; `config/risk_rules.yaml` holds the limits.
Secrets live only in `/etc/desk/desk.env`.

## 3. Key design decisions and why

**The trader is on Anthropic; the analysts are on OpenAI.** The analysts read
the same pack and could share blind spots; putting the role that weighs them
on a different model family means a systematic bias in one family does not
pass through unopposed. It also keeps the expensive, higher-effort call where
judgement matters and the cheap calls where it does not. The original plan had
the analysts on Gemini's free tier; it produced 503 and 429 errors on most
real runs and was replaced. Each role's provider is one config line, so this
is a choice you can revisit, not an architecture.

**Risk is deterministic code, not another model.** A verdict must be
reproducible next week from the numbers. A model-based risk officer would
give verdicts that drift with wording and temperature, and you would never
know whether a vetoed trade was vetoed for a reason. Every verdict cites a
rule id and the values it saw, and exits are never vetoed. The trader sees the
limits in its prompt, so it has to argue within them; a proposal that breaks
one is resized or vetoed and counted against it in the report.

**Every call is recorded; failed runs are not rolled back.** From phase 1 on,
a run that fails halfway has already made paid calls, and the pack is exactly
what the analyst saw. Rolling that back would destroy the evidence at the
moment you most need it. Instead the run row carries the status and consumers
filter on it. `desk report` excludes non-ok runs from the hash comparison and
counts views only for runs that expected them.

**The report compares only runs with the same config hash and commit.** A
code or config change legitimately changes the pack (the ticker record and
the indicator set are inside the hash), so comparing across them would flag a
mismatch on every deploy and you would learn to ignore the check. The config
hash covers `local.yaml` too, so a ticker change shows up as a new group.

**Fundamentals failures are non-fatal but named, never silently empty.** FMP
is optional data: a run without it can still fill, monitor and snapshot. But
an empty section without a reason would let a data gap masquerade as "nothing
to say". So a refused section is left out of that symbol's pack, listed by
name under `fundamentals._unavailable` (names only, to keep hashes stable),
written as a warning on the run row, and the analyst is told which sections
are unavailable and not to assume values for them.

**SIM and LIVE, and why reaching LIVE is not a flag flip.** The desk talks
only to the saxo-mcp HTTP server on the VPS loopback, which is registered as a
SIM app. In the desk itself: `Environment` is an enum with exactly one member,
`SIM`, so a LIVE value does not parse; the runs table has a CHECK constraint
on it; no LIVE URL, token or account key exists in config or env; the guard
aborts on any `SAXO_*LIVE*` variable (which covers saxo-mcp's own
`SAXO_ALLOW_LIVE` switch), on any account key outside your allowlist, and if
the MCP reports trading enabled; and no module constructs an order payload.
Decisions are weights, fills are shadow rows. Reaching LIVE would require a
different Saxo app registration with trading permission, a different account,
a new enum member, new guard logic and an execution module that does not
exist. That is a separate project with its own brief, not a setting.

Smaller decisions worth remembering: models are pinned to explicit versions,
never aliases, because `llm_calls.model` and the config hash must record
exactly what produced each output; a retired model fails loudly as a 404 the
report shows, and `desk discover-models` lists what is served. Stops are
structured, not prose, because a stop code cannot evaluate is not a stop. The
benchmark is SXR8 in EUR rather than SPY because the account is in EUR.

## 4. Verified versus assumed

Verified, with evidence I have seen or you reported:

- Phase 0 exit criterion met on the VPS: two same-day runs with identical
  stable hashes once the pre-fix failed run was excluded.
- Instrument resolution against the live SIM server: `SXR8:xetr` (UIC
  1095726, EUR) and `MSFT:xnas` (UIC 261, USD). Saxo's `ExchangeId` codes
  vary by asset type on one venue; matching on symbol plus MIC is the fix.
- The guard rejects a LIVE env var, an unlisted account key and a
  trading-enabled MCP (tests, and `desk guard` on the VPS).
- Gemini's OpenAI-compatible endpoint and auth style (it answered a bad key
  with "Please pass a valid API key"); its 2.x Flash line is retired.
- The technical analyst produced valid, evidence-grounded views on real data:
  one or two days on Gemini before its free tier failed, then two consecutive
  real `--decide` runs on OpenAI `gpt-5.6-luna` with 6 and 7 evidence items
  and no rejections. That is a handful of runs, not the five consecutive days
  the phase 1 criterion asks for on one model.
- FMP over REST: `desk fmp-check MSFT` passed for all five paths; XIOR
  returns 402 on the two TTM endpoints on every run.
- The test suite: 71 tests, all against fakes of Saxo, FMP, OpenAI-compatible
  and Anthropic responses. They prove the code's logic, not the providers.

Not verified, or tested once, or never seen by me:

- **The trader on Anthropic.** Your two `--decide` runs would have called it,
  but I have not seen a decision chain, a fill or `desk decisions` output from
  a real run. Treat the live Anthropic call, the `claude-sonnet-5` pin and
  the structured-output path as untested until you have looked at one chain.
- **The shadow ledger on real data.** No real fill has been confirmed; the
  fill, snapshot and `desk performance` path has only run against fakes and a
  seeded database.
- **The fundamentals analyst on real data.** It never produced a valid view
  on Gemini and has not yet run on OpenAI.
- **OpenAI request parameters.** `max_completion_tokens` worked (the runs
  succeeded); whether `gpt-5.6-luna` accepts `temperature` is implied by the
  same success but was not checked in isolation.
- **Prices in the cost column** ($1/$6 for Luna, $2/$10 for Sonnet 5) are
  from the request and the SDK docs, marked TODO in config. The usage lines
  under `-v` show the token counts that matter.
- **XIOR's tradability and quotes in SIM.** It resolved and produced packs;
  liquidity and quote quality for a Belgian small cap after the Brussels
  close were not examined.
- **The FMP REST path names** were confirmed for MSFT only.

## 5. Known limitations and open questions

- **FX conversion: fixed 2026-09-17, the day this document was written.**
  Positions were being valued as quantity times the instrument's own price
  and added straight to EUR cash, i.e. a USD position's dollar value was
  treated as if it were euros. That is now corrected: `desk/fx.py` resolves
  the FxSpot pair on Saxo once per run for every currency actually held that
  differs from the account currency, freezes one rate per currency per day in
  a new `fx_rates` table (the same way a price mark is frozen), and every
  fill is converted into account currency at fill time, baked into the
  stored `value`/`fee` so nothing downstream needs to know about currencies
  again. A live mark is converted at read time with that day's rate. A
  currency with no known rate on a given day never gets a guessed value: the
  affected decision stays pending, exactly like a missing price does, and the
  run row carries a warning. See the "FX conversion" section of the README.
  This was verified against the real observed shape of Saxo's FX instruments
  (`EURUSD`, `AssetType FxSpot`, no MIC suffix) and against 11 dedicated
  tests, but has **not yet been observed on a real run** with real fills in
  more than one currency: treat the first real fill of a USD position after
  this fix as something worth reading closely in `desk decisions`.
- **FMP tier gap.** Under your subscription the TTM metrics and ratios
  endpoints are refused for XIOR. The fundamentals analyst argues from
  profile, growth and price targets alone for that name, which is a thinner
  case, and the report will show fewer evidence items. Whether the higher
  tier is worth paying for is a judgement for after you have read a few of
  those views. Expect the same gap on other small-cap non-US names.
- **Two tickers.** Every performance and discipline statistic is on a sample
  of two names and one decision a week. Nothing will be significant for
  months. That is the point of the observation period, but do not read a
  four-week delta as a result.
- **Timing.** Decisions are made after the US close on Monday and fill at
  the next run's quote, roughly Tuesday's close, with a fee of 5 basis points
  and no slippage. Stops are evaluated once a day, not intraday. XIOR's mark
  is a Brussels quote taken hours after that market closed.
- **Analyst views run daily but only Monday's matter.** Daily views cost a
  few cents and give you reading material; they do not change any decision.
  `analysts_every_run: false` turns them off outside decision days.
- **Correlation and sector rules.** Correlation to the book needs the price
  history of every held name, which accumulates from day one. The sector rule
  depends on FMP's profile; it is skipped with a note when the sector is
  unknown.
- **One annualised figure remains.** `desk performance` annualises nothing,
  but the discipline line in `desk report` still shows turnover as a per-year
  rate. On a short window that number is meaningless; ignore it until there
  are months of fills.
- **No alerting.** Nothing pages you. A failed run is a line in `journalctl`
  and a row in the runs table. The most likely silent failure is the Saxo
  session lapsing (saxo-mcp's keep-alive refreshes it, but a refresh token
  can still expire); the run then fails at the guard every day until you run
  `npm run login` in the saxo-mcp directory.
- **Secrets.** The FMP key was pasted into a chat; rotate it in the FMP
  dashboard at some point. Nothing in the repo contains a key.
- **A Saxo LIVE MCP connector exists in your Claude account.** The desk has
  no path to it and I never used it, but it is worth knowing it is there.

## 6. What to do during the observation period

Check in once a week, ten minutes, always the same commands, as the service
user on the VPS:

```
systemctl status desk.timer
journalctl -u desk -n 60
desk report
desk performance
desk decisions --limit 5
desk book
```

What is normal and should be ignored: a `MISSING VIEW` now and then from a
provider 5xx (the retries already happened); XIOR's two 402 warnings on every
run; a hash "mismatch" line straight after a config change (it is grouped by
config hash, so it disappears next day); `TOO EARLY` on the performance report
for the first two weeks; the trader choosing `none` for weeks in a row (that
is discipline, and the rules may be doing exactly what they should).

What is worth interrupting the period for:

- Runs with status `failed` or `guard_failed` three days in a row. Read the
  error on the run line; a guard failure after a Saxo login lapse is fixed by
  `npm run login` and a rerun.
- A decision chain that looks wrong when you read it: a stop that makes no
  sense against the fields, a weight the rules should have capped, a `hold`
  on a name the book does not have. `desk decisions` shows every number the
  rules saw.
- A `WARNING snapshot mismatch` line in `desk performance`. That means the
  ledger and the recomputation disagree, which should never happen.
- `PORTFOLIO_DD_HALT` firing. It means the shadow book is down more than 12%
  from its peak; the rules will refuse new longs until it recovers above 8%.
  Nothing to fix, but worth reading why.
- The same model being rejected on every attempt for days (the report prints
  the last error under the missing view). Usually a retired model id or a
  changed API parameter; both are config.

What not to do: do not tune the risk limits or prompts mid-period because of a
few bad weeks. Every change resets the comparison. Write the idea down and
apply it at the end.

## 7. The forward plan

**Phase 1 and 2 exit, still pending.** Five consecutive daily runs with a
valid view per ticker on the current models; two weeks of daily snapshots
against the benchmark; at least one real decision chain read end to end.
These happen by themselves during the observation period.

**Near term, when credits return (in this order):**

1. Confirm the FX fix on a real run (section 5): the first USD fill after
   2026-09-17 should be read via `desk decisions` and cross-checked against
   the day's actual EUR/USD rate.
2. Read the real decision chains and views. The prompts have never been
   tuned against real output; the first few chains will show whether the
   evidence is what a person would pick and whether the stops are sensible.
3. Widen the universe to three to five names, once the two-ticker loop has
   run cleanly for a few weeks. Prefer names FMP covers fully so the
   fundamentals analyst is on equal footing across the book.
4. The moderator with a counterfactual. On a decision day, ask the trader
   twice: once with the views alone, once with a moderator's list of the
   disagreements between the analysts. Log both proposals, let only the
   configured one reach the gate. After four weeks the database says whether
   the debate ever changed an action. If it never does, remove it; that is a
   result, not a failure.
5. A sentiment analyst only after that, and only if the report's "trader
   sided with" rate shows the two existing analysts are not already
   redundant.

**A distinct later phase: real order execution in SIM.** This is deliberately
separate and later. Everything so far tests reasoning and discipline; this
phase tests execution mechanics, which are a different set of failure modes:
partial fills, rejections, precheck errors, order state, and reconciling the
desk's book against what Saxo's SIM account actually holds. It is the genuine
dry run for what LIVE would one day require, at zero financial risk because
it is SIM. It should start only when the shadow ledger has a track record you
trust, and only when you can watch it, not while credits are out and nobody is
looking. Concretely:

- A Saxo app registration for SIM **with** trading permission, distinct from
  the read-only one the desk uses now, and saxo-mcp started with
  `SAXO_TRADING=enabled` for that app only.
- A second config file, `config/execution.yaml`, that does not exist by
  default; its absence means no execution code path runs. It carries an
  allowlist of tickers, a maximum number of orders per run, and a mode.
- Mode one, for at least a month: `precheck_order` only. Every gate decision
  is dry-run validated against Saxo and the response (estimated cost, margin
  impact, validation errors) is stored next to the decision. Nothing is
  placed. This alone will surface lot sizes, tick sizes and rejections the
  shadow ledger never had to face.
- Mode two: `place_order` for decisions that passed precheck, with a
  propose-and-wait-for-your-approval step for the first weeks, then
  `modify_order` and `cancel_order` for stops and exits. A kill switch that
  halts on any fill the desk did not expect.
- Reconciliation every run: the desk's book versus `get_positions` and
  `get_balances` from the SIM account. Shadow and SIM ledgers run side by
  side; the differences are the finding.
- The guard's `require_trading_disabled` check is relaxed for that app only,
  and the allowlist of account keys stays.

Nothing in that phase touches the SIM/LIVE wall. It uses a SIM account, a SIM
app registration, the same one-member `Environment` enum and the same guard
checks. Real SIM order placement is still zero financial risk and does not
move LIVE any closer.

**LIVE remains a separate, much later, deliberately manual decision.** It
would need its own brief, its own app and account, a new enum member and new
guard logic, a review of every rule with real money in mind, and a period of
running LIVE alongside SIM with tiny size. None of that is scheduled, and
nothing built so far makes it a flag flip. That is by design.
