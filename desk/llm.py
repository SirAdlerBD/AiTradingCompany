"""One call signature for every model role; provider chosen from config.

Providers speak the OpenAI chat-completions wire format, which Gemini exposes at
generativelanguage.googleapis.com/v1beta/openai/ (and Groq, OpenAI, etc. natively).
Anthropic gets its own small adapter when the trader arrives in phase 2.
Nothing here has tools: analysts reason over the data pack, they never fetch.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

from .config import Provider, Role


@dataclass
class LlmResponse:
    text: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    cost_usd: float | None
    raw: Any = None


class LlmClient(Protocol):
    def complete(self, role: Role, system: str, user: str) -> LlmResponse: ...


class LlmError(RuntimeError):
    pass


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n\x00\n" + user).encode()).hexdigest()[:16]


class OpenAICompatClient:
    """Chat completions over HTTP. `transport` is injectable for tests."""

    def __init__(self, providers: dict[str, Provider], transport: Callable[..., httpx.Response] | None = None,
                 retries: int = 2, backoff_s: float = 5.0):
        self.providers = providers
        self.transport = transport
        self.retries = retries
        self.backoff_s = backoff_s

    def complete(self, role: Role, system: str, user: str) -> LlmResponse:
        prov = self.providers.get(role.provider)
        if prov is None:
            raise LlmError(f"role {role.model!r} names unknown provider {role.provider!r}")
        key = os.environ.get(prov.api_key_env)
        if not key:
            raise LlmError(f"{prov.api_key_env} is not set")
        body: dict[str, Any] = {
            "model": role.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": role.temperature,
            "max_tokens": role.max_tokens,
        }
        if role.json_mode:
            body["response_format"] = {"type": "json_object"}
        url = prov.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                if self.transport is not None:
                    resp = self.transport(url, headers=headers, json=body)
                else:
                    resp = httpx.post(url, headers=headers, json=body, timeout=httpx.Timeout(20.0, read=120.0))
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LlmError(f"{prov.name} HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code != 200:
                    raise LlmError(f"{prov.name} HTTP {resp.status_code}: {resp.text[:300]}")  # not retried
                data = resp.json()
                text = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage") or {}
                tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
                cost = None
                if tin is not None and tout is not None:
                    cost = (tin * role.price_in_per_m + tout * role.price_out_per_m) / 1e6
                return LlmResponse(text=text, model=data.get("model") or role.model, tokens_in=tin, tokens_out=tout,
                                   latency_ms=int((time.monotonic() - t0) * 1000), cost_usd=cost, raw=data)
            except (httpx.HTTPError, LlmError, KeyError, ValueError, json.JSONDecodeError) as e:
                last = e
                retryable = isinstance(e, httpx.HTTPError) or (isinstance(e, LlmError) and " HTTP " in str(e)
                                                                and any(f" HTTP {c}" in str(e) for c in (429, 500, 502, 503, 504)))
                if not retryable or attempt == self.retries:
                    break
                time.sleep(self.backoff_s * (attempt + 1))
        raise LlmError(f"{prov.name} call failed: {last}")


def list_models(prov: Provider, get: Callable[..., httpx.Response] | None = None) -> list[str]:
    """GET {base_url}/models. Used by `desk discover-models` to check a pin is still served."""
    key = os.environ.get(prov.api_key_env)
    if not key:
        raise LlmError(f"{prov.api_key_env} is not set")
    url = prov.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {key}"}
    resp = (get or httpx.get)(url, headers=headers, timeout=30.0)
    if resp.status_code != 200:
        raise LlmError(f"{prov.name} HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json().get("data", [])
    names = [str(m.get("id", "")).removeprefix("models/") for m in data if isinstance(m, dict)]
    return sorted(n for n in names if n)


def parse_json_object(text: str) -> dict[str, Any]:
    """Tolerate a ```json fence or leading prose; the payload must still be one JSON object."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in response")
    obj = json.loads(s[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("response JSON is not an object")
    return obj
