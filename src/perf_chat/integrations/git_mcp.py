"""Provider-neutral Git MCP gateway with separate service and repository credentials."""

import hashlib
import json

from .mcp_gateway import MCPGateway


def git_token_destination(url, repository):
    """Bind personal credentials to both MCP endpoint and repository."""
    return "git-mcp:" + hashlib.sha256(json.dumps(
        [url, repository or ""], separators=(",", ":"),
    ).encode()).hexdigest()


class MCPGitGateway(MCPGateway):
    """Reuse discovery, schema validation, allowlists and transport-only Bearer auth.

    No repository/argument names or hosting-provider APIs are assumed. Repository
    identity and revision semantics remain explicit limitations of generic MCP data.
    """

    def __init__(self, url, timeout=60, *, token="", allowed_tools=None,
                 caller=None, tool_lister=None):
        super().__init__(url, timeout, token=token, allowed_tools=allowed_tools,
                         caller=caller, tool_lister=tool_lister)

    def call_tool(self, tool, arguments, *, result_id=""):
        return super().call_tool(tool, arguments, result_id="")
