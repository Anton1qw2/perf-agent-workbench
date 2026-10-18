# chats

A local workspace for defining performance optimization tasks through conversation
and delegating execution to external infrastructure. Web and terminal clients
share an API, conversation history and task state.

Two LangGraph agents handle conversational task intake and structured specification extraction.
A profile analyzer can inspect metrics through MCP and add read-only Git/GitLab
source context. The default executor is a **demo**; it performs no real optimization.

## Quick start

Requires Python 3.11+ and Git. Clone this repository and run from its root
(PowerShell). See [deployment](docs/deployment.md) for clone commands and Linux/macOS setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
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

Set Repository and Branch in **Project settings**, describe the goal, constraints
and acceptance criteria in chat, then click **Run**. Profile is optional; profile
analysis uses it as the MCP server's `result_id`. Reply to clarification questions
in chat. Launch requires a task summary and no unresolved questions.

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
GitLab and profiler tokens in Project settings; never paste tokens into chat.
Tokens are encrypted on the backend. Authenticated endpoints require HTTPS except
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
.\.venv\Scripts\python.exe -m ruff check src tests
node --test tests/web_state.test.cjs
```

JavaScript tests require Node.js 18+. The Web client needs no build step.
Regenerate schemas after changing contracts with `python scripts/export_contracts.py`.

## Documentation and limitations

- [Deployment guide](docs/deployment.md): installation, startup checks, addresses and storage.
- [Integration and operations guide](docs/integration-guide.md): credentials, source
  context, profiler APIs, reusable agents, executor contracts and persistence behavior.
- [Source context design](docs/source-context-plan.md): source enrichment and planning reviews.
- [Project guide](AGENTS.md): architecture and development rules.

This MVP serves one local user without authentication or user isolation. Demo data
is synthetic. Private MCP response formats and real provider/service integrations
still require deployment validation. Perf documentation starts empty in
`src/perf_chat/profiler_docs_agent/Perf.md`; add reference material to enable lookups.
