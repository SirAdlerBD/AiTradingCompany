"""Config loading. Environment is an enum with one member on purpose."""
from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Environment(str, Enum):
    SIM = "SIM"  # LIVE is not a member. Adding it is a separate project.


class RestFetch(BaseModel):
    """One REST GET whose result becomes a section of the data pack (fundamentals.<name>)."""
    path: str                                       # relative to fmp_rest.base_url
    params: dict[str, Any] = Field(default_factory=dict)
    keep: list[str] = Field(default_factory=list)   # field allowlist; [] keeps everything
    limit: int | None = None                        # keep at most N rows when the result is a list


class FmpRest(BaseModel):
    enabled: bool = False
    base_url: str = "https://financialmodelingprep.com/stable"
    api_key_env: str = "FMP_API_KEY"
    api_key_param: str = "apikey"
    symbol_param: str = "symbol"
    timeout_s: float = 30.0
    retries: int = Field(default=2, ge=0, le=10)
    backoff_s: float = Field(default=5.0, ge=0)
    fetch: dict[str, RestFetch] = Field(default_factory=dict)


class McpServer(BaseModel):
    url: str
    enabled: bool = True
    auth_style: Literal["bearer", "query", "header"] = "bearer"
    auth_env: str
    auth_param: str = "apikey"          # query parameter / header name for query|header styles
    symbol_arg: str = "symbol"          # argument name the server's per-ticker tools take
    tools: dict[str, str] = Field(default_factory=dict)          # named tools (saxo)
    sim_account_keys_env: str | None = None
    require_trading_disabled: bool = True

    def secret(self) -> str | None:
        return os.environ.get(self.auth_env) or None


class Ticker(BaseModel):
    symbol: str
    mic: str        # listing as the suffix of Saxo's Symbol field: MSFT:xnas -> xnas
    currency: str


class Universe(BaseModel):
    tickers: list[Ticker]
    history_days: int = Field(default=250, ge=1, le=1200)


class Benchmark(BaseModel):
    symbol: str
    mic: str
    currency: str
    start_capital: float = Field(gt=0)


class Storage(BaseModel):
    db_path: Path
    reports_dir: Path | None = None       # default: <db dir>/reports


class Provider(BaseModel):
    name: str = ""
    kind: Literal["openai_compat", "anthropic"] = "openai_compat"
    base_url: str = ""                  # OpenAI-compatible chat-completions root (openai_compat only)
    api_key_env: str                    # name of the env var, never the key
    retries: int = Field(default=3, ge=0, le=10)     # on 429/5xx/network
    backoff_s: float = Field(default=20.0, ge=0)     # first wait; doubles each retry
    # openai_compat request quirks: OpenAI's newer models take max_completion_tokens
    # and some reject sampling parameters; Gemini's endpoint takes the classic names.
    max_tokens_param: str = "max_tokens"
    send_temperature: bool = True


class Role(BaseModel):
    kind: Literal["analyst", "trader"] = "analyst"
    prompt: str = ""                    # analyst prompt key (technical, fundamentals) or a prompt_file
    prompt_file: str | None = None      # optional path to a system prompt that replaces the built-in one
    sections: list[str] = Field(default_factory=list)   # pack sections the role sees; [] = prompt default
    provider: str
    model: str
    temperature: float = 0.2            # openai_compat only; Anthropic models reject sampling params
    max_tokens: int = 1500
    effort: str | None = None           # anthropic only: low | medium | high | xhigh | max
    json_mode: bool = True
    price_in_per_m: float = 0.0         # USD per 1M tokens, for the cost column only
    price_out_per_m: float = 0.0


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class DecisionCadence(BaseModel):
    weekdays: list[str] = Field(default_factory=lambda: ["Mon"])   # [] = every run
    min_days_between: int = Field(default=0, ge=0)                # 0 = no minimum

    @field_validator("weekdays")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d not in WEEKDAYS]
        if bad:
            raise ValueError(f"unknown weekday(s) {bad}; use {WEEKDAYS}")
        return v


class Pipeline(BaseModel):
    analysts: list[str] = Field(default_factory=list)   # analyst role names, run per ticker
    analysts_every_run: bool = True                     # False = only on decision runs
    trader: str | None = None                           # trader role name; None = no decisions
    decision: DecisionCadence = Field(default_factory=DecisionCadence)
    max_attempts: int = Field(default=2, ge=1, le=4)     # per model answer; rejections are fed back


class RiskConfig(BaseModel):
    rules_file: Path = Path("config/risk_rules.yaml")


class LedgerConfig(BaseModel):
    fee_bps: float = Field(default=5.0, ge=0)           # per fill, on traded value
    allow_fractional: bool = True                       # shadow book may hold fractional shares
    mark: Literal["mid", "last"] = "mid"


class Config(BaseModel):
    environment: Environment
    saxo_mcp: McpServer
    fmp_rest: FmpRest = Field(default_factory=FmpRest)
    universe: Universe
    benchmark: Benchmark
    storage: Storage
    providers: dict[str, Provider] = Field(default_factory=dict)
    roles: dict[str, Role] = Field(default_factory=dict)
    pipeline: Pipeline = Field(default_factory=Pipeline)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    raw_hash: str = ""
    config_dir: Path = Path(".")
    local_path: Path | None = None       # config/local.yaml when present (per-user values, gitignored)

    @model_validator(mode="after")
    def _wire(self) -> "Config":
        for name, prov in self.providers.items():
            prov.name = prov.name or name
            if prov.kind == "openai_compat" and not prov.base_url:
                raise ValueError(f"provider {name!r} is openai_compat but has no base_url")
        wanted = list(self.pipeline.analysts) + ([self.pipeline.trader] if self.pipeline.trader else [])
        for r in wanted:
            if r not in self.roles:
                raise ValueError(f"pipeline names unknown role {r!r}")
            if self.roles[r].provider not in self.providers:
                raise ValueError(f"role {r!r} names unknown provider {self.roles[r].provider!r}")
        for r in self.pipeline.analysts:
            if self.roles[r].kind != "analyst":
                raise ValueError(f"pipeline.analysts role {r!r} has kind {self.roles[r].kind!r}")
        if self.pipeline.trader and self.roles[self.pipeline.trader].kind != "trader":
            raise ValueError(f"pipeline.trader role {self.pipeline.trader!r} must have kind trader")
        return self

    def path(self, p: Path | str) -> Path:
        """Resolve a path from config relative to the config file's directory."""
        p = Path(p)
        return p if p.is_absolute() else self.config_dir / p


LOCAL_NAME = "local.yaml"


def deep_merge(base: Any, over: Any) -> Any:
    """Dicts merge key by key, recursively; anything else (lists, scalars) is replaced by `over`."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = deep_merge(base.get(k), v) if k in base else v
        return out
    return over


def load(path: str | Path = "config/desk.yaml", local: str | Path | None = None) -> Config:
    """Load config/desk.yaml (repo-managed) and merge config/local.yaml (per-user, gitignored)
    over it when it exists next to it. The config hash covers both files, so a ticker or
    effort change in local.yaml is a visible config change in the runs table."""
    path = Path(path)
    text = path.read_text()
    data = yaml.safe_load(text) or {}
    local_path = Path(local) if local else path.parent / LOCAL_NAME
    local_text = ""
    if local_path.exists():
        local_text = local_path.read_text()
        data = deep_merge(data, yaml.safe_load(local_text) or {})
    cfg = Config(**data)
    cfg.raw_hash = hashlib.sha256((text + "\n---local---\n" + local_text).encode()).hexdigest()[:16]
    cfg.config_dir = path.resolve().parent.parent if path.parent.name == "config" else path.resolve().parent
    cfg.local_path = local_path.resolve() if local_text else None
    return cfg


def effective_yaml(cfg: Config) -> str:
    """The merged config as YAML, for `desk config`. Secrets are never in config, only env var names."""
    data = cfg.model_dump(mode="json", exclude={"raw_hash", "config_dir", "local_path"})
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
