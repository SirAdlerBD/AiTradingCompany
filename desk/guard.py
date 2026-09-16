"""Startup guard. Runs before anything touches the network for real work.

Checks, all cheap, all fatal on failure:
1. config.environment == SIM (the enum makes anything else unparseable anyway)
2. no LIVE-looking SAXO_* variable is present in this process's environment
   (saxo-mcp's own live switch is SAXO_ALLOW_LIVE, which this catches)
3. get_account_summary: every AccountKey the server reports is on the SIM
   allowlist from the environment, and the server reports trading DISABLED
   (its SAXO_TRADING hard block is still in place) until phase 5 lifts that.
"""
from __future__ import annotations

import os
from typing import Any

from .config import Config, Environment
from .mcp_client import McpClient


class GuardFailure(RuntimeError):
    pass


FORBIDDEN_ENV_SUBSTRINGS = ("LIVE",)


def check_environment(cfg: Config) -> None:
    if cfg.environment is not Environment.SIM:
        raise GuardFailure(f"environment={cfg.environment}, only SIM is allowed")
    for k in os.environ:
        if k.upper().startswith("SAXO") and any(s in k.upper() for s in FORBIDDEN_ENV_SUBSTRINGS):
            raise GuardFailure(f"forbidden env var present: {k}")


def allowlist(cfg: Config) -> set[str]:
    keys_env = cfg.saxo_mcp.sim_account_keys_env or ""
    return {k.strip() for k in os.environ.get(keys_env, "").split(",") if k.strip()}


def check_summary(cfg: Config, summary: Any, allow: set[str]) -> str:
    """Pure check on a get_account_summary payload. Returns the default account key."""
    if not allow:
        raise GuardFailure(
            f"{cfg.saxo_mcp.sim_account_keys_env} is empty; refusing to run without a SIM allowlist"
        )
    if not isinstance(summary, dict):
        raise GuardFailure(f"unexpected account summary shape: {type(summary).__name__}")

    trading = str(summary.get("trading", ""))
    if cfg.saxo_mcp.require_trading_disabled and not trading.upper().startswith("DISABLED"):
        raise GuardFailure(f"MCP server reports trading is not disabled: {trading[:80]!r}")

    accounts = summary.get("accounts") or []
    keys = [str(a.get("AccountKey")) for a in accounts if isinstance(a, dict) and a.get("AccountKey")]
    if not keys:
        raise GuardFailure("account summary lists no AccountKey")
    rogue = sorted(set(keys) - allow)
    if rogue:
        raise GuardFailure(f"account key(s) not in SIM allowlist: {rogue}")

    client = summary.get("client") or {}
    default = str(client.get("DefaultAccountKey") or keys[0])
    if default not in allow:
        raise GuardFailure(f"default account key {default!r} not in SIM allowlist")
    return default


async def check_account(cfg: Config, saxo: McpClient) -> str:
    summary = await saxo.call(cfg.saxo_mcp.tools["account_summary"], {})
    return check_summary(cfg, summary, allowlist(cfg))
