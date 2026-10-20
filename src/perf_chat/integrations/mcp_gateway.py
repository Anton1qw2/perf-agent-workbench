"""Shared bounded Streamable HTTP MCP discovery, validation and calls."""

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..profiler_analysis_agent.contracts import MCPTool
from ..runtime import check_deadline, remaining_timeout
from .mcp_schema import validate_schema


def validate_token_destination(value):
    url = urlsplit(value)
    host = url.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if (not host or url.username or url.password or url.fragment or "\\" in value
            or not (url.scheme == "https" or (url.scheme == "http" and loopback))):
        raise ValueError("MCP tokens require HTTPS or loopback HTTP")


def bounded_payload(data):
    if len(json.dumps(data, allow_nan=False)) > 1_000_000:
        raise ValueError("MCP response exceeds the analysis limit")
    return data


def decode_tool_result(result):
    if result.isError:
        raise ValueError("MCP tool reported an error")
    data = result.structuredContent
    if data is None:
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if not text.strip():
            raise ValueError("MCP returned no text or structured data")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
    return bounded_payload(data)


def tool_definition(tool):
    return MCPTool(
        name=tool.name, description=tool.description or "",
        input_schema=tool.inputSchema, output_schema=tool.outputSchema,
    )


class MCPGateway:
    demo = False

    def __init__(
        self,
        url: str,
        timeout: float = 60,
        caller: Callable[[str, dict], Any] | None = None,
        token: str = "",
        *, tool_lister: Callable[[], list[dict]] | None = None,
        allowed_tools: list[str] | None = None,
        profile_arguments: dict[str, str] | None = None,
    ):
        self.url = url
        self.timeout = timeout
        self.caller = caller
        self._token = token
        self.tool_lister = tool_lister
        self.allowed_tools = frozenset(allowed_tools or [])
        # Keep explicit old constraints compatible; never infer argument names here.
        self.profile_arguments = dict(profile_arguments or {})

    @property
    def configuration_fingerprint(self):
        # Pin endpoint and operator policy without persisting URL parameters or tokens.
        payload = {
            "endpoint": self.url, "allowed_tools": sorted(self.allowed_tools),
            "profile_arguments": self.profile_arguments,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def auth_headers(self):
        if self._token:
            validate_token_destination(self.url)
        return {"Authorization": "Bearer " + self._token} if self._token else {}

    async def _invoke(self, name, arguments, expected_tool=None):
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
                    for tool in tools.tools:
                        if tool.name == name:
                            if expected_tool is not None and tool_definition(tool) != expected_tool:
                                raise ValueError("MCP tool definition changed; start a new run")
                            return decode_tool_result(await session.call_tool(name, arguments))
                    cursor = tools.nextCursor
                    if not cursor:
                        break
                raise ValueError(f"Required profiler MCP tool is unavailable: {name}")

    async def _discover(self):
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError("Install perf-chat[mcp] for MCP tool discovery") from exc
        async with (
            httpx.AsyncClient(headers=self.auth_headers(), follow_redirects=False,
                              timeout=remaining_timeout(self.timeout)) as client,
            streamable_http_client(self.url, http_client=client) as (read, write, _),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                cursor, items = None, []
                for _ in range(20):
                    page = await session.list_tools(cursor=cursor)
                    items.extend(tool_definition(tool).model_dump() for tool in page.tools)
                    if len(items) > 100:
                        raise ValueError("MCP tool catalog exceeds 100 tools")
                    bounded_payload(items)
                    cursor = page.nextCursor
                    if not cursor:
                        return items
                raise ValueError("MCP tool catalog pagination limit exceeded")

    def list_tools(self) -> list[MCPTool]:
        timeout = remaining_timeout(self.timeout)
        if self.tool_lister is not None:
            items = self.tool_lister()
            check_deadline()
        else:
            if self.caller:
                raise ValueError("Injected dynamic transports need tool_lister")

            async def bounded():
                return await asyncio.wait_for(self._discover(), timeout=timeout)

            items = asyncio.run(bounded())
        bounded_payload(items)
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("MCP tool catalog must contain at most 100 tools")
        tools = [MCPTool.model_validate(item) for item in items]
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("MCP tool names must be unique")
        return tools

    def call_tool(self, tool: MCPTool, arguments: dict, *, result_id: str = ""):
        if tool.name not in self.allowed_tools:
            raise ValueError("MCP tool is not allowed by backend policy")
        validate_schema(arguments, tool.input_schema)
        binding = self.profile_arguments.get(tool.name)
        if binding and arguments.get(binding) != result_id:
            raise ValueError("MCP tool arguments do not match the current profile")
        timeout = remaining_timeout(self.timeout)
        if self.caller:
            current = next((item for item in self.list_tools() if item.name == tool.name), None)
            if current != tool:
                raise ValueError("MCP tool definition changed; start a new run")
            data = self.caller(tool.name, arguments)
            check_deadline()
            data = bounded_payload(data)
        else:
            async def bounded():
                return await asyncio.wait_for(
                    self._invoke(tool.name, arguments, expected_tool=tool), timeout=timeout,
                )

            data = asyncio.run(bounded())
        check_deadline()
        if tool.output_schema is not None:
            validate_schema(data, tool.output_schema)
        return data
