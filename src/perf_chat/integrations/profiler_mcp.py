"""Profiler MCP gateway and synthetic fixture using the shared MCP transport."""

from ..profiler_analysis_agent.contracts import MCPTool
from .mcp_gateway import MCPGateway
from .mcp_gateway import decode_tool_result as decode_tool_result
from .mcp_gateway import validate_token_destination as validate_token_destination


class MCPProfilerGateway(MCPGateway):
    """Profiler transport; optional legacy profile-argument constraints remain supported."""


class DemoProfilerGateway(MCPProfilerGateway):
    """Synthetic MCP fixture; uses the same discovery/call contract as real servers."""

    demo = True

    def __init__(self):
        catalog = [MCPTool(
            name="sample_measurements", description="Read synthetic sample measurements.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ).model_dump()]
        super().__init__(
            "demo://synthetic", allowed_tools=[item["name"] for item in catalog],
            tool_lister=lambda: catalog,
            caller=lambda name, arguments: {
                "demo": True, "rows": [{"function": "demo_function", "metric": "cpu_samples",
                                         "value": 120, "unit": "samples"}],
            },
        )
