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
    def complete(self, role: Role, system: str, user: str, schema: type | None = None) -> LlmResponse: ...


class LlmError(RuntimeError):
    pass


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n\x00\n" + user).encode()).hexdigest()[:16]


class OpenAICompatClient:
    """Chat completions over HTTP. `transport` is injectable for tests."""

    def __init__(self, providers: dict[str, Provider], transport: Callable[..., httpx.Response] | None = None,
                 retries: int | None = None, backoff_s: float | None = None):
        self.providers = providers
        self.transport = transport
        self._retries = retries        # None = per-provider config
        self._backoff = backoff_s

    def complete(self, role: Role, system: str, user: str, schema: type | None = None) -> LlmResponse:
        prov = self.providers.get(role.provider)
        if prov is None:
            raise LlmError(f"role {role.model!r} names unknown provider {role.provider!r}")
        retries = prov.retries if self._retries is None else self._retries
        backoff = prov.backoff_s if self._backoff is None else self._backoff
        key = os.environ.get(prov.api_key_env)
        if not key:
            raise LlmError(f"{prov.api_key_env} is not set")
        body: dict[str, Any] = {
            "model": role.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            prov.max_tokens_param: role.max_tokens,
        }
        if prov.send_temperature:
            body["temperature"] = role.temperature
        if role.json_mode:
            body["response_format"] = {"type": "json_object"}
        url = prov.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        last: Exception | None = None
        tries = 0
        for attempt in range(retries + 1):
            tries += 1
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
                if not retryable or attempt == retries:
                    break
                time.sleep(backoff * (2 ** attempt))
        raise LlmError(f"{prov.name} call failed after {tries} attempt(s): {last}")


class AnthropicClient:
    """Trader-grade calls through the official Anthropic SDK with structured output.

    `client_factory` is injectable for tests. Sampling parameters are never sent
    (current Claude models reject them); depth is controlled by `effort`.
    """

    def __init__(self, providers: dict[str, Provider], client_factory: Callable[[Provider], Any] | None = None):
        self.providers = providers
        self.client_factory = client_factory
        self._clients: dict[str, Any] = {}

    def _client(self, prov: Provider):
        if prov.name not in self._clients:
            if self.client_factory is not None:
                self._clients[prov.name] = self.client_factory(prov)
            else:
                import anthropic
                key = os.environ.get(prov.api_key_env)
                if not key:
                    raise LlmError(f"{prov.api_key_env} is not set")
                self._clients[prov.name] = anthropic.Anthropic(api_key=key, max_retries=prov.retries)
        return self._clients[prov.name]

    def complete(self, role: Role, system: str, user: str, schema: type | None = None) -> LlmResponse:
        prov = self.providers.get(role.provider)
        if prov is None:
            raise LlmError(f"role {role.model!r} names unknown provider {role.provider!r}")
        client = self._client(prov)
        kwargs: dict[str, Any] = {
            "model": role.model, "max_tokens": role.max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if role.effort:
            kwargs["output_config"] = {"effort": role.effort}
        t0 = time.monotonic()
        try:
            if schema is not None:
                resp = client.messages.parse(output_format=schema, **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - SDK errors become one LlmError with the class name
            raise LlmError(f"{prov.name} {type(e).__name__}: {str(e)[:300]}") from None
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LlmError(f"{prov.name} refused the request ({getattr(resp, 'stop_details', None)})")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise LlmError(f"{prov.name} hit max_tokens={role.max_tokens} before finishing")
        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "input_tokens", None)
        tout = getattr(usage, "output_tokens", None)
        cost = None if tin is None or tout is None else (tin * role.price_in_per_m + tout * role.price_out_per_m) / 1e6
        return LlmResponse(text=text, model=getattr(resp, "model", role.model), tokens_in=tin, tokens_out=tout,
                           latency_ms=int((time.monotonic() - t0) * 1000), cost_usd=cost, raw=resp)


class Router:
    """Dispatches each role to its provider's client. Build one per run."""

    def __init__(self, providers: dict[str, Provider], clients: dict[str, LlmClient] | None = None):
        self.providers = providers
        self.clients: dict[str, LlmClient] = clients or {}

    def _for(self, role: Role) -> LlmClient:
        prov = self.providers.get(role.provider)
        if prov is None:
            raise LlmError(f"unknown provider {role.provider!r}")
        if prov.kind not in self.clients:
            self.clients[prov.kind] = (AnthropicClient(self.providers) if prov.kind == "anthropic"
                                       else OpenAICompatClient(self.providers))
        return self.clients[prov.kind]

    def complete(self, role: Role, system: str, user: str, schema: type | None = None) -> LlmResponse:
        return self._for(role).complete(role, system, user, schema)


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
