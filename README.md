# IPA — Intelligent Performance Assistant

IPA collects application optimization goals, constraints and acceptance criteria
through conversation, and orchestrates analysis agents and external execution
infrastructure. Web and terminal clients share an API, conversation history and
task state.

The Web interface uses the shorter name **Performance Assistant**. The Python
package remains `perf_chat`, and the distribution remains `perf-chat`.

Two LangGraph agents handle conversational task intake and structured specification extraction.
A profile analyzer can inspect metrics through MCP and add read-only Git MCP or local Git
source context. The default executor is a **demo**; it performs no real optimization.

## Quick start

Requires Python 3.11+ and Git. Clone this repository and run from its root
(PowerShell). See [deployment](docs/deployment.md) for clone commands and Linux/macOS setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The lock file was generated on Windows with Python 3.12. Start the API and worker
in separate terminals using the same environment and database:

```powershell
# Terminal 1: API and Web
.\.venv\Scripts\python.exe -m perf_chat.api

# Terminal 2: intake agents and executor
.\.venv\Scripts\python.exe -m perf_chat.worker

# Optional third terminal: TUI
.\.venv\Scripts\python.exe -m perf_chat.ui.tui
```

Open [Web](http://127.0.0.1:8000) or [API docs](http://127.0.0.1:8000/docs).
Demo intake works without model credentials.

## Usage and configuration

Ask "What can you do?" to see supported functions, the configured execution mode
and registered consultation experts without changing the current task. This
description does not verify backend connectivity. See
[consultation experts](docs/consultation-experts.md) for routing and extension details.

Provide either a Git Repository or a Profile in **Project settings**, describe the
goal, constraints and acceptance criteria in chat, then click **Run**. Both inputs
can be supplied together. Profile analysis uses Profile as the MCP `result_id` and
does not require a repository or branch. Source access may require a branch.
Launch requires a task summary and no unresolved intake questions. The selected
executor requests additional data during the run when needed; a profile analyzer
may ask for a result ID after a repository-only launch. Reply in chat.

Chat messages support Markdown in Web and TUI. Web also renders tables and fenced
code blocks; raw HTML is escaped and remote images are not loaded. The Web execution
status panel shows two lines with internal scrolling; Copy copies its full text.
Working states show animated blue dots and a thin activity bar. Errors use muted red,
completed runs use green, and clarification/cancellation/reconnection stay neutral.
Animation stops while waiting for user input or reconnecting and respects reduced-motion
preferences. A green completion status does not establish a performance improvement.

TUI commands include `/new`, `/open ID`, `/run`, `/cancel` and
`/set target.repository=...`. F2 toggles project settings.

Configure `.env` for both API and worker:

| Setting | Options |
| --- | --- |
| `PERF_CHAT_MODE` | `demo` for deterministic intake; `llm` for model-based conversation |
| `PERF_CHAT_EXECUTOR` | `demo`, `profile-demo` for synthetic analysis, or `profile-mcp` for real read-only profiler queries |
| `PERF_CHAT_DB` | Shared SQLite database path |
| `PERF_CHAT_API_HOST` / `PERF_CHAT_API_PORT` | API listener; defaults to `127.0.0.1:8000` |
| `PERF_CHAT_API_URL` | TUI connection URL; defaults to `http://127.0.0.1:8000` |

For real profiler queries, install `.[mcp]` and set `PROFILER_MCP_URL`. Manage
Git/profiler tokens in Project settings; Git MCP endpoint and permissions use ENV.
Never paste tokens into chat. Saved tokens are encrypted on the backend. Authenticated endpoints require HTTPS except
for localhost and literal loopback addresses. See the integration guide for setup.

## Connect your LLM

Use a hosted provider or a self-hosted inference server with an OpenAI-compatible
Chat Completions API. Configure the existing `.env` in the project root:

```dotenv
PERF_CHAT_MODE=llm
LLM_BASE_URL=https://inference.example.com/v1
LLM_MODEL=your-served-model-id
LLM_API_KEY=your-provider-key
LLM_TIMEOUT_SECONDS=90
```

- `LLM_BASE_URL` is the API base, including `/v1` if required by your server. The
  adapter appends `/chat/completions`; do not include that suffix in the base URL.
- `LLM_MODEL` must exactly match the model ID or deployment alias accepted by the server.
- `LLM_API_KEY` is sent as `Authorization: Bearer ...`. Leave it empty only when the
  server allows unauthenticated requests. Use HTTPS for remote endpoints.
- The server must accept `system`/`user` messages and
  `response_format={"type":"json_object"}`, and return nonempty text in
  `choices[0].message.content`. A native provider API needs a compatible gateway.
- The timeout must be greater than zero and at most 120 seconds. The remaining run
  deadline can shorten it. No provider SDK or frontend API key is required.

For a local server, use its actual base URL, for example `http://127.0.0.1:8080/v1`.
Restart API and worker with the same settings, then send a task and confirm an agent
reply. See [LLM connection checks](docs/deployment.md#llm-connection-checks) for
configuration precedence, container networking and troubleshooting. LLM mode enables
intake and profile interpretation; `PERF_CHAT_EXECUTOR` separately selects execution
and defaults to `demo`. Keep keys out of chat and version control.

## Consultation experts

The main chat separates reference questions from optimization task intake.
Consultations do not change the task or require repository/profile settings.
The default expert answers profiler documentation questions; backend code can add
hardware or other experts through ExpertRegistry and Worker(..., experts=...).
Use `/ask <expert_id> <question>` for explicit selection and `/task <text>` to return
to task intake. In LLM mode routing also uses expert descriptions and conversation
context. Pending expert questions survive Worker restart. Guidance does not establish
measurements or agreement to change a task. No registration UI is included.
See [backend consultation experts](docs/consultation-experts.md) for a working
registration example, context policies, limits and retry requirements.

## Replaceable profile analyzer

In `profile-mcp` mode, the built-in analyzer discovers MCP tools dynamically and uses
LLM planning within a bounded investigation workflow. Configure `PROFILER_MCP_URL`
and `PROFILER_MCP_ALLOWED_TOOLS` for your server. Arguments come from discovered MCP
input schemas and task context; no argument map is required. Unknown essential values
are clarified in chat. The default allowlist is empty; discovery does not authorize
calls. Active runs pin the endpoint and tool policy; changing either requires a new run. `profile-demo` uses this same graph with synthetic MCP data and a model fixture.
The planner chooses profiler calls, optional pinned source reads, clarification or
completion. A final formatting node produces bounded Markdown for Web/TUI. Full raw
evidence stays in run details. Unstructured or invalid model output is retained and
shown only as an explicitly unvalidated excerpt, never accepted as a finding. Tokens remain transport-only. The old fixed analyzer has been removed.
Replace the analyzer in code with `Worker(settings, analyzer_factory=your_factory)`.
See [analyzer configuration and custom graphs](docs/analyzer-extension.md).
During analysis, the agent can request a new profile, repository or branch and ask
for confirmation before applying it. Versioned updates preserve history and budgets;
replacement/comparison keep evidence scopes separate. See [context updates](docs/analyzer-context.md).

## Git MCP integration

To connect any Git hosting system through MCP, install `.[mcp]` and configure:

```dotenv
GIT_MCP_URL=https://git-tools.example.com/mcp
GIT_MCP_ALLOWED_TOOLS=["your_read_only_repository_tool"]
GIT_MCP_TIMEOUT_SECONDS=60
GIT_MCP_TOKEN=
```

Use `PERF_CHAT_EXECUTOR=profile-mcp` and LLM mode. The backend discovers schemas and
builds arguments; no provider-specific API or manual argument map is required.
Enter the repository reference, optional branch and optional Git access token in
Project settings. A saved token is sent as Bearer to Git MCP, with priority over
the ENV token. Active runs preserve their saved token snapshot. Git-only
analysis needs no profiler Result ID or endpoint. Tokens stay in backend headers.
The shared analyzer namespaces tools by server, validates calls and preserves raw
evidence. Generic MCP responses do not prove immutable commit identity or profile
linkage. See [Git MCP setup and limitations](docs/git-mcp.md).

## External execution infrastructure

Implement `ExecutorGateway.step(run_id, task, cursor, answer)` and `cancel(run_id)`,
then supply the gateway to `Worker(settings, executor=YourGateway(...))`.
`step` returns an execution event and a JSON-serializable cursor. The external
infrastructure owns graph state, checkpoints and retry-safe side effects.

[contracts.py](src/perf_chat/contracts.py) defines the integration models;
[schemas/](schemas/) contains exported JSON schemas. The API queues work, the worker
executes it, and SQLite persists tasks, runs and events. Clients use HTTP/SSE.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
node --test tests/web_state.test.cjs
```

JavaScript tests require Node.js 18+. The Web client needs no build step.
Regenerate schemas after changing contracts with `python scripts/export_contracts.py`.

## Documentation and limitations

- [Deployment guide](docs/deployment.md): installation, startup checks, addresses and storage.
- [Integration and operations guide](docs/integration-guide.md): credentials, source
  context, profiler APIs, reusable agents, executor contracts and persistence behavior.
- [Git MCP guide](docs/git-mcp.md): provider-neutral repository tools and ENV settings.
- [Local source context](docs/integration-guide.md#git-source-context): immutable source evidence and limits.
- [Consultation experts](docs/consultation-experts.md): backend registration and context policies.
- [Analyzer extensions](docs/analyzer-extension.md): replaceable analysis lifecycle.
- [Context updates](docs/analyzer-context.md): confirmed changes during analysis.
- [Project guide](AGENTS.md): architecture and development rules.
- [Review notes](REVIEW.md): dynamic analyzer findings, fixes and validation.

This MVP serves one local user without authentication or user isolation. Demo data
is synthetic. Private MCP response formats and real provider/service integrations
require deployment validation. The profiler knowledge base starts empty in
`src/perf_chat/profiler_docs_agent/Perf.md`; replace it with your reference material.
See [documentation setup and citations](docs/integration-guide.md#profiler-documentation-expert)
for full-document limits and source links, and
[expert routing](docs/consultation-experts.md#profiler-documentation-expert) for
LLM selection and demo trigger configuration.
