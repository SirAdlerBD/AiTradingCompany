"""Financial Modeling Prep over plain REST. Code fetches; the API key never leaves the process env.

FMP's hosted MCP server authenticates with OAuth, not the API key, so this
module talks to the REST API it wraps: GET {base_url}/{path}?symbol=...&apikey=...
Each configured fetch becomes one section of the data pack, reduced to its
`keep` fields so intraday price/volume noise stays out of the stable hash.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

from .config import Config, FmpRest


class FmpFailure(RuntimeError):
    pass


class FmpClient:
    def __init__(self, cfg: FmpRest, get: Callable[..., httpx.Response] | None = None):
        self.cfg = cfg
        self.get = get or httpx.get
        key = os.environ.get(cfg.api_key_env)
        if not key:
            raise FmpFailure(f"{cfg.api_key_env} is not set")
        self.key = key

    def fetch(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.cfg.base_url.rstrip('/')}/{path.lstrip('/')}"
        q = dict(params)
        q[self.cfg.api_key_param] = self.key
        last: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                resp = self.get(url, params=q, timeout=self.cfg.timeout_s)
            except httpx.HTTPError as e:
                last = e
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as e:
                        raise FmpFailure(f"{path}: response is not JSON: {resp.text[:120]}") from e
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = FmpFailure(f"{path}: HTTP {resp.status_code}")
                else:
                    raise FmpFailure(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")
            if attempt < self.cfg.retries:
                time.sleep(self.cfg.backoff_s * (2 ** attempt))
        raise FmpFailure(f"{path}: failed after {self.cfg.retries + 1} attempt(s): {last}")

    def fundamentals(self, symbol: str) -> dict[str, Any]:
        """Every configured section that succeeds, plus `_unavailable`: the sorted names of
        sections that did not (e.g. a 402 for an endpoint outside the FMP subscription).
        The reasons land in `self.last_errors` for the run's warning; only names go into
        the pack so the stable hash does not depend on error wording. Raises FmpFailure
        only when every section fails."""
        out: dict[str, Any] = {}
        self.last_errors: list[str] = []
        unavailable: list[str] = []
        for name, spec in self.cfg.fetch.items():
            params = dict(spec.params)
            params[self.cfg.symbol_param] = symbol
            try:
                raw = self.fetch(spec.path, params)
            except FmpFailure as e:
                unavailable.append(name)
                self.last_errors.append(f"{name}: {classify(str(e))}")
                continue
            out[name] = reduce_rows(raw, spec.keep, spec.limit)
        if unavailable and not out:
            raise FmpFailure("; ".join(self.last_errors))
        if unavailable:
            out["_unavailable"] = sorted(unavailable)
        return out

    def check(self, symbol: str) -> list[dict[str, Any]]:
        """For `desk fmp-check`: per fetch, what came back and which keep fields are missing."""
        report = []
        for name, spec in self.cfg.fetch.items():
            params = dict(spec.params)
            params[self.cfg.symbol_param] = symbol
            row: dict[str, Any] = {"name": name, "path": spec.path}
            try:
                raw = self.fetch(spec.path, params)
                rows = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
                keys = sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
                row.update(ok=True, rows=len(rows), keys=keys, missing=[k for k in spec.keep if k not in keys])
                if isinstance(raw, dict) and "Error Message" in raw:
                    row.update(ok=False, error=str(raw["Error Message"])[:200])
            except FmpFailure as e:
                row.update(ok=False, error=classify(str(e))[:220])
            report.append(row)
        return report


def classify(error: str) -> str:
    """Name the failure class in plain words. A 402 is FMP saying the endpoint is outside
    the subscription for this symbol; that is a data-tier gap, not a bug to work around."""
    if "HTTP 402" in error:
        return "subscription tier (HTTP 402, endpoint not included for this symbol): " + error
    if "HTTP 404" in error:
        return "unknown path or symbol (HTTP 404): " + error
    if "HTTP 401" in error or "HTTP 403" in error:
        return "key rejected: " + error
    return error


def reduce_rows(raw: Any, keep: list[str], limit: int | None) -> Any:
    rows = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    if limit is not None:
        rows = rows[:limit]
    if keep:
        rows = [{k: r.get(k) for k in keep} for r in rows if isinstance(r, dict)]
    return rows[0] if len(rows) == 1 else rows
