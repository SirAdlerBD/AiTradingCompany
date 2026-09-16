"""Config loading. Environment is an enum with one member on purpose."""
from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Environment(str, Enum):
    SIM = "SIM"  # LIVE is not a member. Adding it is a separate project.


class McpServer(BaseModel):
    url: str
    enabled: bool = True
    auth_style: Literal["bearer", "query", "header"] = "bearer"
    auth_env: str
    auth_param: str = "apikey"          # query parameter / header name for query|header styles
    symbol_arg: str = "symbol"          # argument name the server's per-ticker tools take
    tools: dict[str, str] = Field(default_factory=dict)
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


class Provider(BaseModel):
    name: str = ""
    base_url: str                       # OpenAI-compatible chat-completions root
    api_key_env: str                    # name of the env var, never the key


class Role(BaseModel):
    provider: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 1500
    json_mode: bool = True
    price_in_per_m: float = 0.0         # USD per 1M tokens, for the cost column only
    price_out_per_m: float = 0.0


class Pipeline(BaseModel):
    analysts: list[str] = Field(default_factory=list)   # role names to run per ticker; [] = phase 0
    max_attempts: int = Field(default=2, ge=1, le=4)     # per view, rejected answers are fed back once


class Config(BaseModel):
    environment: Environment
    saxo_mcp: McpServer
    fmp_mcp: McpServer
    universe: Universe
    benchmark: Benchmark
    storage: Storage
    providers: dict[str, Provider] = Field(default_factory=dict)
    roles: dict[str, Role] = Field(default_factory=dict)
    pipeline: Pipeline = Field(default_factory=Pipeline)
    raw_hash: str = ""

    @model_validator(mode="after")
    def _wire(self) -> "Config":
        for name, prov in self.providers.items():
            prov.name = prov.name or name
        for r in self.pipeline.analysts:
            if r not in self.roles:
                raise ValueError(f"pipeline.analysts names unknown role {r!r}")
            if self.roles[r].provider not in self.providers:
                raise ValueError(f"role {r!r} names unknown provider {self.roles[r].provider!r}")
        return self


def load(path: str | Path = "config/desk.yaml") -> Config:
    text = Path(path).read_text()
    cfg = Config(**yaml.safe_load(text))
    cfg.raw_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    return cfg
