"""Config loading. Environment is an enum with one member on purpose."""
from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


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


class Config(BaseModel):
    environment: Environment
    saxo_mcp: McpServer
    fmp_mcp: McpServer
    universe: Universe
    benchmark: Benchmark
    storage: Storage
    roles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    raw_hash: str = ""


def load(path: str | Path = "config/desk.yaml") -> Config:
    text = Path(path).read_text()
    cfg = Config(**yaml.safe_load(text))
    cfg.raw_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    return cfg
