"""SQLite store. Every table carries run_id so any decision is replayable."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,              -- running | ok | failed | guard_failed
  environment   TEXT NOT NULL CHECK (environment = 'SIM'),
  config_hash   TEXT NOT NULL,
  risk_version  INTEGER,
  git_commit    TEXT,
  account_key   TEXT,
  error         TEXT
);

-- Saxo UIC + AssetType per (symbol, mic), resolved once via search_instruments.
-- `exchange` holds the MIC from config (xnas, xetr); Saxo's own ExchangeId is not stored.
CREATE TABLE IF NOT EXISTS instruments (
  symbol      TEXT NOT NULL,
  exchange    TEXT NOT NULL,
  uic         INTEGER NOT NULL,
  asset_type  TEXT NOT NULL,
  currency    TEXT,
  description TEXT,
  saxo_symbol TEXT,
  resolved_at TEXT NOT NULL,
  PRIMARY KEY (symbol, exchange)
);

-- Frozen inputs. stable_hash covers everything the LLMs will see; the
-- live quote is stored but excluded so two same-day runs hash identically.
CREATE TABLE IF NOT EXISTS data_packs (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  ticker        TEXT NOT NULL,
  stable_json   TEXT NOT NULL,
  stable_hash   TEXT NOT NULL,
  volatile_json TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, ticker)
);

-- Daily bars kept long-term for correlation rules and benchmark maths.
CREATE TABLE IF NOT EXISTS price_history (
  ticker  TEXT NOT NULL,
  date    TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (ticker, date)
);

-- Every model call, phase 1+. No exceptions.
CREATE TABLE IF NOT EXISTS llm_calls (
  id           INTEGER PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs(run_id),
  ticker       TEXT,
  role         TEXT NOT NULL,
  model        TEXT NOT NULL,
  prompt_hash  TEXT NOT NULL,
  prompt       TEXT NOT NULL,
  response     TEXT NOT NULL,
  tokens_in    INTEGER, tokens_out INTEGER,
  latency_ms   INTEGER, cost_usd REAL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyst_views (
  id            INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id), ticker TEXT NOT NULL,
  role          TEXT NOT NULL,
  thesis        TEXT NOT NULL,
  evidence_json TEXT NOT NULL,   -- [{field, value, source}] into the data pack
  confidence    REAL,
  would_be_wrong_if TEXT NOT NULL,
  llm_call_id   INTEGER REFERENCES llm_calls(id)
);

CREATE TABLE IF NOT EXISTS debate_turns (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id), ticker TEXT NOT NULL,
  round INTEGER NOT NULL, speaker TEXT NOT NULL,
  disagreements_json TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
  llm_call_id INTEGER REFERENCES llm_calls(id)
);

CREATE TABLE IF NOT EXISTS trader_proposals (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id), ticker TEXT NOT NULL,
  action        TEXT NOT NULL CHECK (action IN ('long','hold','exit','none')),
  target_weight REAL NOT NULL,
  winning_argument TEXT NOT NULL,
  rejected_json TEXT NOT NULL,
  stop_condition TEXT NOT NULL,
  llm_call_id INTEGER REFERENCES llm_calls(id)
);

CREATE TABLE IF NOT EXISTS risk_verdicts (
  id INTEGER PRIMARY KEY,
  proposal_id   INTEGER NOT NULL REFERENCES trader_proposals(id),
  rules_checked_json TEXT NOT NULL,
  rule_fired    TEXT,                        -- rule id or NULL
  verdict       TEXT NOT NULL CHECK (verdict IN ('pass','resize','veto')),
  original_weight REAL NOT NULL, adjusted_weight REAL NOT NULL,
  numbers_json  TEXT NOT NULL                -- the values that triggered it
);

-- The execution gate writes here and nowhere else (phase 2-4).
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id), ticker TEXT NOT NULL,
  proposal_id INTEGER NOT NULL REFERENCES trader_proposals(id),
  verdict_id  INTEGER NOT NULL REFERENCES risk_verdicts(id),
  action TEXT NOT NULL, final_weight REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY,
  decision_id INTEGER NOT NULL REFERENCES decisions(id),
  source  TEXT NOT NULL CHECK (source IN ('shadow','sim')),
  ticker TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL,
  filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  date TEXT PRIMARY KEY,
  cash REAL NOT NULL, positions_json TEXT NOT NULL, total_value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_snapshots (
  date TEXT PRIMARY KEY,
  symbol TEXT NOT NULL, currency TEXT, price REAL NOT NULL,
  units REAL NOT NULL,                       -- start_capital / price on day 0
  value REAL NOT NULL,
  run_id TEXT REFERENCES runs(run_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def j(obj) -> str:
    """Canonical JSON: sorted keys, no whitespace, so hashes are stable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
