"""Thin MCP client. Code calls tools; LLMs never do (phase 0-3).

Wraps mcp.Client (SDK 2.x) over Streamable HTTP. Auth is attached by the
orchestrator from an env var named in config, so the token never sits in YAML.
For tests, pass an in-process MCPServer as `inproc` and no network is touched.
"""
from __future__ import annotations

import json
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .config import McpServer


class McpToolError(RuntimeError):
    pass


class McpConnectError(RuntimeError):
    pass


def _leaves(eg: BaseException) -> list[BaseException]:
    if isinstance(eg, BaseExceptionGroup):
        return [leaf for e in eg.exceptions for leaf in _leaves(e)]
    return [eg]


class McpClient:
    def __init__(self, server: McpServer, name: str, inproc: Any | None = None):
        self.server = server
        self.name = name
        self._inproc = inproc
        self._http: httpx2.AsyncClient | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> "McpClient":
        if self._inproc is not None:
            self._client = Client(self._inproc)
        else:
            url = self.server.url
            headers: dict[str, str] = {}
            secret = self.server.secret()
            if secret:
                if self.server.auth_style == "bearer":
                    headers["Authorization"] = f"Bearer {secret}"
                elif self.server.auth_style == "header":
                    headers[self.server.auth_param] = secret
                else:  # query
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}{self.server.auth_param}={secret}"
            self._http = httpx2.AsyncClient(
                headers=headers, timeout=httpx2.Timeout(30.0, read=120.0)
            )
            await self._http.__aenter__()
            self._client = Client(streamable_http_client(url, http_client=self._http))
        try:
            await self._client.__aenter__()
        except BaseException as e:  # noqa: BLE001 - re-raised below in a readable form
            leaves = _leaves(e)
            if leaves and all(isinstance(x, (httpx2.HTTPError, OSError)) for x in leaves):
                if self._http is not None:
                    await self._http.__aexit__(None, None, None)
                raise McpConnectError(
                    f"[{self.name}] cannot reach MCP server at {self.server.url}: {leaves[0]}"
                ) from None
            raise
        return self

    async def __aexit__(self, *exc) -> bool:
        try:
            if self._client is not None:
                try:
                    await self._client.__aexit__(*exc)
                except BaseExceptionGroup as eg:
                    # The SDK's task group re-raises the exception we are already
                    # unwinding with, wrapped in a group. Let the original propagate
                    # bare so callers can `except GuardFailure`.
                    if exc[1] is None or _leaves(eg) != [exc[1]]:
                        raise
        finally:
            if self._http is not None:
                await self._http.__aexit__(*exc)
        return False

    async def list_tools(self) -> list[dict[str, Any]]:
        assert self._client is not None
        res = await self._client.list_tools()
        out = []
        for t in res.tools:
            schema = t.input_schema if hasattr(t, "input_schema") else getattr(t, "inputSchema", {})
            out.append({
                "name": t.name,
                "description": t.description or "",
                "args": sorted((schema or {}).get("properties", {}).keys()),
            })
        return out

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Call a tool and return parsed JSON (structured content if the server sends it)."""
        assert self._client is not None
        if not tool:
            raise McpToolError(f"[{self.name}] tool name not configured")
        res = await self._client.call_tool(tool, args or {})
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        text = "\n".join(texts)
        if res.is_error:
            raise McpToolError(f"[{self.name}] {tool} failed: {text[:800]}")
        if res.structured_content:
            return res.structured_content
        # One block holding a JSON document (saxo-mcp, FMP), or one block per list
        # item (the Python SDK serialises a list return that way). Both become JSON.
        try:
            if len(texts) == 1:
                return json.loads(texts[0])
            return [json.loads(t) for t in texts]
        except json.JSONDecodeError:
            return text
