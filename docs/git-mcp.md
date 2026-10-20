# Provider-neutral Git MCP

The built-in analyzer can query a Git MCP server alongside the profiler MCP server.
It does not require GitLab, GitHub or another hosting provider's REST API. The Git
MCP service owns provider access; the workbench discovers its schemas and executes
only the read-only tools explicitly permitted by the operator.

## Configure the backend

Install the MCP extra with `python -m pip install -e ".[mcp]"`. Configure the same
environment for API and worker, including your existing LLM settings:

```dotenv
PERF_CHAT_MODE=llm
PERF_CHAT_EXECUTOR=profile-mcp
LLM_BASE_URL=https://inference.example.com/v1
LLM_MODEL=your-served-model-id
LLM_API_KEY=your-provider-key
GIT_MCP_URL=https://git-tools.example.com/mcp
GIT_MCP_ALLOWED_TOOLS=["your_repository_reader","your_symbol_search"]
GIT_MCP_TIMEOUT_SECONDS=60
GIT_MCP_TOKEN=
```

Use names actually exposed by your server. The allowlist defaults to `[]`, permitting
no calls. No manual argument map is required. Tool descriptions or read-only
annotations do not authorize calls; the operator must trust the permitted tools
and configure repository access on the MCP server itself. Do not allow modifying,
cloning, fetching or code-execution operations.

Authentication follows the profiler integration. Enter an optional **Git access
token** in Repository settings. It is encrypted on the backend and sent as
`Authorization: Bearer ...` to the configured Git MCP endpoint, never in prompts or
tool arguments. The Git MCP server must support that authentication scheme and
decides how the credential grants access to the underlying Git provider.
`GIT_MCP_TOKEN` is an optional ENV fallback when no matching saved token is bound
to the run. A saved personal token takes precedence; only one Bearer header is sent.
HTTPS is required when using a token, except
for localhost or literal loopback HTTP addresses. Authentication is checked again
before sending headers; redirects are not followed. Keep `.env` private and out of
version control. Restart both processes after configuration changes.

The supported transport is MCP Streamable HTTP. The client opens a new session for
discovery/calls; tools must work with durable identifiers rather than session-local
handles. Stdio/SSE-only services require an adapter. Timeouts must be finite, greater
than zero and at most 120 seconds, and are clamped to the remaining run deadline.

## Run an analysis

Enter the repository URL or server-specific repository reference and optional branch
in Project settings. Describe the goal in chat and click Run. A repository-only
task can use Git MCP without a profiler Result ID or `PROFILER_MCP_URL`. When both
inputs and endpoints are configured, the planner can use both servers.

Known limitation: if the run starts without a Result ID, providing one later in
chat does not enable profiler tools in that same run. Start a new run with Profile
supplied to combine both evidence sources. This behavior is retained by user decision.

The analyzer discovers permitted tool schemas, supplies known repository/revision
context, and constructs arguments itself. Unknown essential values are requested
in chat. Tools are namespaced by `server=git` or `server=profiler`, so identical names
on the two servers do not collide. Discovery does not expose the token to the model.
`profile-demo` remains synthetic and never connects to Git MCP.

The standalone CLI also reads Git MCP settings:

```powershell
python -m perf_chat.profiler_analysis_agent --repository "team/project" --branch main --goal "Inspect allocation paths"
```

For custom analyzers, the factory receives the gateway as `AnalysisServices.git`.
Use its `list_tools()` and `call_tool()` methods; enforce your own bounded algorithm,
repository scope, citation rules and checkpoint lifecycle. No UI/API changes are
needed to replace the built-in analyzer.

## Evidence, durability and limitations

Each call is planned and checkpointed before execution. The endpoint/allowlist
fingerprint is pinned separately for Git and profiler MCP; changing either requires
a new run. Selected tool definitions and arguments are revalidated before execution.
Schema resolution cannot fetch remote documents. Shared limits include eight MCP
calls and sixteen planning rounds by default, plus the common model input budget.
Catalog limits apply independently to each server: 100 tools, 20 pages and 1000000
serialized JSON characters per catalog/response. Calls may be redelivered after a
crash, which is why permitted operations must be read-only and retry-safe.

Git tool results have `Evidence.server="git"`; reports store their catalog in
`git_tool_catalog`. Raw results, timestamps, hashes and evidence IDs are persisted
in run details and cited through the same validation/Markdown boundary as profiler
evidence. Repository responses are untrusted reference data, not instructions or
runtime measurements. Completion does not prove a performance improvement.

Generic schemas do not establish which argument identifies a repository or commit.
The planner is instructed to resolve and reuse an immutable commit when supported,
but the host cannot certify repository scope, commit/blob identity or profile-build
linkage from arbitrary JSON. Configure server permissions to enforce access scope.
Missing identity guarantees remain explicit report limitations. MCP responses are
not converted into host-verified `SourceEvidence` or accepted as replacements for
required local source ranges. Stronger guarantees require a schema-specific adapter
or custom analyzer. `source.policy="off"` disables Git MCP discovery/calls as well
as local source reads.

The local Git adapter remains available for registered aliases/explicit paths and
provides pinned, bounded UTF-8 source evidence. The built-in worker no longer selects
a GitLab REST adapter. Old saved GitLab tokens are never forwarded to Git MCP.
The `credentials.git_token` API field now creates separate Git MCP credential
records; it can also clear legacy bindings without migrating their secret values.
The old standalone GitLab module remains import-compatible but is outside the
active analysis path. Existing v2 analyzer runs must restart; saved reports remain
readable with backward-compatible default fields. ENV token rotation takes effect
on restart; this service credential is not a per-conversation credential snapshot.

Personal tokens are bound to both the exact Git MCP endpoint and repository. Blank
inputs preserve them; Remove and Save clears future bindings. Active runs use
immutable encrypted credential references, so rotation/removal does not change their
saved personal token. Changing the endpoint or repository never forwards an old
personal token. Unused records are pruned after completion/cancellation/failure.
Without a saved token, changing ENV fallback credentials takes effect on restart.

Local tests cover routing, schemas, policy drift, restart and secret isolation.
Private Git MCP servers and real LLM planning require deployment validation.
