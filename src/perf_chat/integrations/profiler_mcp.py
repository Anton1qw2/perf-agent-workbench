"""Adapter for get_list_metrics/get_hotspots/get_asm over MCP Streamable HTTP."""

import asyncio
import ipaddress
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..runtime import check_deadline, remaining_timeout


def validate_token_destination(value):
    url = urlsplit(value)
    host = url.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if (not host or url.username or url.password or url.fragment or "\\" in value
            or not (url.scheme == "https" or (url.scheme == "http" and loopback))):
        raise ValueError("Profiler tokens require HTTPS or loopback HTTP")


def bounded_payload(data):
    if len(json.dumps(data, allow_nan=False)) > 1_000_000:
        raise ValueError("Profiler MCP response exceeds the analysis limit")
    return data


def decode_tool_result(result):
    if result.isError:
        raise ValueError("Profiler MCP tool reported an error")
    data = result.structuredContent
    if data is None:
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if not text.strip():
            raise ValueError("Profiler MCP returned no text or structured data")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
    return bounded_payload(data)


class MCPProfilerGateway:
    demo = False

    def __init__(
        self,
        url: str,
        timeout: float = 60,
        caller: Callable[[str, dict], Any] | None = None,
        token: str = "",
    ):
        self.url = url
        self.timeout = timeout
        self.caller = caller
        self._token = token

    def auth_headers(self):
        if self._token:
            validate_token_destination(self.url)
        return {"Authorization": "Bearer " + self._token} if self._token else {}

    async def _invoke(self, name, arguments):
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError(
                "Install perf-chat[mcp] to connect to a profiler MCP server"
            ) from exc
        async with (
            httpx.AsyncClient(
                headers=self.auth_headers(),
                follow_redirects=False,
                timeout=remaining_timeout(self.timeout),
            ) as client,
            streamable_http_client(self.url, http_client=client) as (read, write, _),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                cursor = None
                for _ in range(20):
                    tools = await session.list_tools(cursor=cursor)
                    if name in {tool.name for tool in tools.tools}:
                        return decode_tool_result(await session.call_tool(name, arguments))
                    cursor = tools.nextCursor
                    if not cursor:
                        break
                raise ValueError(f"Required profiler MCP tool is unavailable: {name}")

    def _call(self, name, arguments):
        timeout = remaining_timeout(self.timeout)
        if self.caller:
            data = self.caller(name, arguments)
            check_deadline()
            return bounded_payload(data)

        async def bounded():
            return await asyncio.wait_for(self._invoke(name, arguments), timeout=timeout)

        return asyncio.run(bounded())

    def get_list_metrics(self, result_id: str):
        return self._call("get_list_metrics", {"result_id": result_id})

    def get_hotspots(self, result_id: str, metrics: list[str]):
        return self._call("get_hotspots", {"result_id": result_id, "metrics": metrics})

    def get_asm(self, result_id: str, metrics: list[str]):
        return self._call("get_asm", {"result_id": result_id, "metrics": metrics})


class DemoProfilerGateway:
    """Synthetic data only; never opens a profile or contacts a server."""

    demo = True

    def get_list_metrics(self, result_id: str):
        return ["cpu_samples"]

    def get_hotspots(self, result_id: str, metrics: list[str]):
        return {
            "demo": True,
            "rows": [
                {
                    "function": "demo_function",
                    "metric": metrics[0],
                    "value": 120,
                    "unit": "samples",
                },
            ],
        }

    def get_asm(self, result_id: str, metrics: list[str]):
        return {"demo": True, "text": "Synthetic assembly placeholder; no code was disassembled."}
