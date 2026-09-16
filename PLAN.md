# Build plan

A research desk that runs against a Saxo **SIM** account only, decides on a
configured cadence, holds for weeks to months, and is measured on discipline
first and alpha last. Each phase has an exit criterion that is checked from the
database, not from feelings. Status is kept current in this file.

| Phase | What it is | Done when | Status |
|---|---|---|---|
| 0 | Skeleton: guard, hashed data pack, benchmark, SQLite, systemd timer | Two same-day runs hash identically | Done |
| 1 | One technical analyst; output validated against the pack | Five consecutive daily runs with a valid view per ticker | Running since 2026-09-16 |
| 2 | The loop: trader, risk rules in code, gate, shadow ledger, stops, time stop | Any decision reconstructible from the DB (`desk decisions`); shadow book marked daily against the benchmark for two weeks (`desk performance`) | Built, observing |
| 3 | Breadth and debate: fundamentals analyst on FMP, 3 to 5 tickers, one moderator pass | Four weeks with debate on, and DB evidence of decisions the debate changed | Fundamentals analyst built; FMP wiring and moderator pending |
| 4 | Live in SIM, untouched, 8 to 12 weeks | Monthly review of discipline KPIs: veto rate, turnover, holding period, drawdown vs benchmark | |
| 5 | SIM execution through the Saxo MCP, behind its own unlock ritual | Shadow and SIM ledgers reconciled side by side | |

Sentiment analyst: after phase 4, and only if the two existing analysts are not
already redundant (see the "trader sided with" rate in `desk report`).

## How a run works (phase 2)

```
guard -> data packs (Saxo bars + quote, FMP fundamentals) -> analyst views
      -> fill pending decisions at today's mark (shadow ledger)
      -> monitor open positions: stop, time stop -> exit decisions via the gate
      -> on a decision day: per ticker, trader -> risk (code) -> gate -> pending decision
      -> portfolio snapshot, benchmark snapshot
```

Everything that can vary is in `config/desk.yaml`: the roster of roles and
their models, which roles run, the decision cadence (weekdays and a minimum
gap), the risk limits (`config/risk_rules.yaml`), fees, marking, the FMP
fetches that feed the fundamentals section. Nothing about cadence or limits is
hardcoded.

## Doors that stay closed

- `Environment` is an enum with one member. LIVE is not an option that is off;
  it does not exist in this codebase.
- No module constructs an order payload. Decisions are weights, fills are
  shadow rows. Phase 5 adds execution behind a second config file that does
  not exist by default.
- The guard aborts if the Saxo MCP reports trading enabled, if any account key
  is off the SIM allowlist, or if any `SAXO_*LIVE*` variable is present.
- Analysts and the trader have no tools. They reason over a frozen pack.
