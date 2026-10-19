# Project guide

## Purpose

The product is named **chats**; the Python package is `perf_chat` and the distribution
is `perf-chat`. It is a local MVP for defining performance optimization tasks through
a conversation and handing them to an external execution workflow.

Web and TUI clients share one API, conversation history and task state. Two intake
agents use LangGraph: the task agent builds a structured specification, and the
orchestrator communicates with the user. The actual optimization agents are developed
separately and connect through `ExecutorGateway`.

## Architecture and ownership

```text
src/perf_chat/
  ui/
    web/                 Vanilla HTML, CSS and JavaScript; no build step
    tui.py               Textual terminal interface
    client.py            HTTP/SSE client for the TUI
  orchestrator/
    agent.py             User-facing orchestration agent
    graph.py             LangGraph intake workflow
  task_agent/
    agent.py             Structured task extraction and updates
  profiler_docs_agent/
    agent.py             Profiler documentation expert and ask_perf_docs tool
    retrieval.py         Full Markdown snapshot and section source registry
    Perf.md             Initially empty UTF-8 knowledge base, packaged with the module
  profiler_analysis_agent/  LangGraph metric analysis, contracts, validation and executor bridge
  integrations/
    profiler_mcp.py      get_list_metrics/get_hotspots/get_asm MCP adapter and synthetic fixture
    git_source.py        Bounded read-only access to registered local Git repositories
    gitlab_source.py     Authenticated read-only GitLab API access to pinned objects
  contracts.py           Pydantic models and executor boundary
  api.py                 FastAPI REST endpoints, SSE and static assets
  worker.py              Durable queue processing outside HTTP requests
  store.py               SQLite persistence, versions, jobs, runs and events
  executor.py            Demonstration executor gateway
  llm.py                 OpenAI-compatible Chat Completions adapter
  config.py              Environment configuration
  credentials.py         Backend-only DPAPI/Fernet token encryption
  runtime.py             Shared execution deadline enforcement
schemas/                 Exported contract schemas
scripts/export_contracts.py
tests/                   Python and Node regression tests
```

Keep UI, orchestrator and task agent in their separate directories. Clients access
the backend through HTTP/SSE; they must not call agents or access SQLite directly.
The API queues work; the worker executes intake and executor steps.

The intake graph consults the documentation expert when the latest user message
matches a literal keyword or phrase in `profiler_docs_agent/triggers.txt`. Read this
UTF-8 file each turn, ignoring blank/comment lines and matching case-insensitive word
boundaries. Missing, unreadable or empty configuration disables automatic chat routing.
Package the trigger file alongside the replaceable `Perf.md` reference document.
The expert is also reusable by execution agents through `ask()` or `as_tool()`.
The expert receives the full UTF-8 document, the current question and optional version/
platform only, never task state, metrics, repository code or conversation history.
Do not retrieve snippets or silently truncate the document. Bound the complete serialized
model request using `PERF_CHAT_DOCS_MAX_INPUT_BYTES` (default 2000000); this byte budget
is separate from the analyzer context budget and does not establish provider token capacity.
Keep documentation as untrusted reference data. The first standalone Markdown link on
the first content line after a heading supplies the section's HTTP(S) source URL;
subsections inherit parent URLs. Parse headings outside fenced code; retain a content hash.
Use registry IDs and render validated section source links, never Markdown line citations.
Missing source URLs are explicitly local documentation. Return only the concise expert
answer and cited source metadata downstream, never the full document. Empty, unavailable,
unsupported or oversized documentation must produce an explicit limitation. Demo mode
reports no synthesis. The expert does not run a profiler, browse links or establish
measured results. Documentation edits are read on each lookup.

## Product behavior

- Documentation, code comments, test examples, interface labels, built-in messages
  and agent prompts use English.
- The main visual accent is blue. Preserve the current compact layout and shared
  message-composer frame with its Send button at the bottom right.
- Web status badges use calm red for errors, green for completed runs and blue for
  work in progress. Animate dots and the thin header bar only during active work;
  clarification and reconnection are static. Respect reduced-motion preferences.
  A snapshot response must not clear an SSE disconnect notice before reconnection.
- Both sidebars collapse to narrow clickable rails. Their expansion states are
  remembered independently. On narrow screens, expanded panels behave as overlays.
- Project settings group Repository (URL/alias, Branch, GitLab token) and Profiler
  (Result ID, API token). Both groups collapse independently and remember state.
  Profile is an optional opaque reference. The profile-analysis executor treats it
  as the MCP server's result_id. It does not open local files automatically.
- Clarify goals, constraints, acceptance criteria and optimization details through
  chat. Do not add a universal mandatory metrics questionnaire or technical form.
- Store the task description in `summary`, agreed parameters in `details`, and material
  unresolved questions in `open_questions`. Launch readiness requires a nonempty
  summary, no open questions, and either a repository or a profile. Do not require
  both inputs or a branch for profile-only analysis. Ask for a branch only when
  source resolution needs it. The selected executor requests missing analysis data
  after launch; do not require both inputs at intake just because one executor
  needs a profiler result ID.
- Keep legacy optional contract fields compatible with existing saved data.

## Execution and persistence rules

`contracts.py` is the source of truth for integration models. `PerformanceTask`
versions become snapshots for runs. `ExecutionEvent` reports progress, clarification
or a result. Clarification replies are correlated through `request_id`.

An executor implements `ExecutorGateway.step(run_id, task, cursor, answer)` and
`cancel(run_id)`, and is supplied to `Worker`. The gateway owns its execution graph
state and any mapping from run IDs to LangGraph thread IDs. The bundled executor is
a demo: never present its output as real measurements or completed optimization.
LLM mode changes the intake agents; it does not replace the demo executor.

`PERF_CHAT_EXECUTOR` selects `demo` (original scenario), `profile-demo` (synthetic
analysis), or `profile-mcp` (real read-only MCP queries). API and worker must agree on
the setting. The built-in `profile-mcp` analyzer dynamically discovers tools and uses
bounded LLM planning. `profile-demo` preserves the deterministic three-method graph.
Authorize read-only tools through `PROFILER_MCP_ALLOWED_TOOLS`; annotations/descriptions
are untrusted and do not grant permission. `PROFILER_MCP_PROFILE_ARGUMENTS` configures
per-tool top-level argument binding to the current profile ID. Validate input/output
schemas without network schema retrieval and reject changed tool definitions.
`ProfileAnalyzer.step_analysis` / `AnalysisStep` is the replaceable lifecycle boundary;
pass `analyzer_factory` to `Worker`. Factories receive per-run `AnalysisServices` and
must restore their graph from the JSON checkpoint or a persistent external checkpointer.
Preserve `analyzer_id` on the run cursor and the catalog, decisions and evidence in
`run.cursor.analyzer_state`. Pin the gateway's `configuration_fingerprint` (endpoint,
allowlist and profile-argument mapping) there as well; never store raw endpoint query
parameters or tokens in that fingerprint field. Fail closed on connection/policy drift,
including between a planned call and its execution. Do not silently switch analyzer
implementations during active runs.
Pass ordered question/answer pairs to both planning and final interpretation within
the common model input budget. User clarifications are context, not measured evidence.
Use one structural evidence check for explicit finish and budget exhaustion: null,
empty arrays/objects and whitespace-only text cannot alone justify interpretation.
Preserve zero/false responses without inventing their semantics. See
`docs/analyzer-extension.md` for configuration and custom graph integration.
Do not add a calculations module. Preserve server values and units; do not invent
missing metric semantics. Private server response schemas still need deployment validation. Reports separate observations and
hypotheses, retain raw evidence, and are persisted in `run.cursor.analysis`.
Keep conversation run summaries lightweight; retrieve full cursor/evidence through
the dedicated run endpoint. Model calls have a total timeout. Profiler calls and
documentation/model calls share the run deadline; late success must be rejected.
Use retry-stable IDs distinct for each new clarification, and preserve corrected
analysis options across questions. Current task summary/profile override stale
copies in free-form details.

Source enrichment uses `details.profile_analysis.source` and typed source contracts.
Register local aliases through `PERF_CHAT_GIT_REPOSITORIES`; never clone/fetch or
change a user's working tree. The executor persists source preparation outcomes
(resolved/unavailable/disabled) before analysis; preserve them across all cursor
transitions and never re-resolve after acceptance. Source options cannot override
task repository/branch. An explicit commit includes matching scope_repository/ref.
Missing source defaults to optional; required source selection is clarified in chat.
Do not infer source availability from the private MCP's unknown response schema.
Source text candidates never prove hotspot identity, and commit/build linkage remains
unverified. Only regular UTF-8 Git blobs from registered local aliases or GitLab HTTPS URLs are supported; do not follow links,
execute code, hydrate LFS or load unbounded process output. Preserve source whitespace,
the exact delivered line ranges and content hashes. `context_builder.py` applies one model input budget
and citation registry across metrics, docs and source. Keep instruction Markdown in
package data and explicitly load it; repository text is untrusted evidence.

SQLite WAL stores durable jobs and events. Jobs use leases and can be redelivered
after a failure. Executor integrations must tolerate retries, including distinguishing
a step awaiting clarification from its subsequent resume with an answer. Cancellation
is cooperative. Only one active run is allowed per conversation.

Preserve optimistic task version checks, request deduplication and atomic state/event
updates. Preserve per-conversation drafts, protection against late HTTP responses,
SSE connection ownership and snapshot retries after reconnects. Do not erase agreed
task fields when an LLM returns a partial object.

## Local setup and startup

Requirements: Python 3.11+; Node.js 18+ for JavaScript tests. Run commands from this
project directory. The locked environment was generated with Python 3.12 on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Use separate terminals with the same working directory and environment:

```powershell
.\.venv\Scripts\python.exe -m perf_chat.api
.\.venv\Scripts\python.exe -m perf_chat.worker
.\.venv\Scripts\python.exe -m perf_chat.ui.tui
```

The default API/Web address is `http://127.0.0.1:8000`. Configure the listener
with `PERF_CHAT_API_HOST` and `PERF_CHAT_API_PORT`. A development preview may use
another port; configure the TUI's `PERF_CHAT_API_URL` accordingly. API and worker
must use the same `PERF_CHAT_DB`.

### LLM connection contract

`PERF_CHAT_MODE=demo` makes no model calls. To connect a hosted or self-hosted model,
configure the existing project-root `.env` or backend process environment:

```dotenv
PERF_CHAT_MODE=llm
LLM_BASE_URL=https://inference.example.com/v1
LLM_MODEL=your-served-model-id
LLM_API_KEY=your-provider-key
LLM_TIMEOUT_SECONDS=90
```

`llm.py` sends a non-streaming POST to `{LLM_BASE_URL}/chat/completions`. Include the
server's API prefix (commonly `/v1`) in the base URL, but not `/chat/completions`.
Use the exact model ID/deployment alias accepted by the server. Local servers may
use a loopback HTTP URL; remote servers should use HTTPS. The URL must be reachable
from the backend process; container loopback does not refer to the host machine.

The compatibility requirements are:

- Accept `model` and `messages` with `system` and `user` roles.
- Support `response_format={"type":"json_object"}` for structured task extraction
  and analysis. Ordinary conversational text alone is insufficient.
- Return a nonempty string at `choices[0].message.content`; parsed JSON is validated
  against application contracts. Streaming-only, tool-call-only and native provider
  APIs require an adapter or compatibility gateway.
- Accept Bearer authentication if enabled: a nonempty `LLM_API_KEY` adds an
  `Authorization` header. An empty key omits that header and works only if the server
  permits it. Do not require a dummy key for an unauthenticated local server.

Timeouts must be finite, greater than zero and at most 120 seconds; calls also obey
the remaining execution deadline. No provider SDK is required. API and worker must
use the same configuration and be restarted after changes. `load_dotenv()` does not
override existing process environment variables.

Verify `/api/health` reports `mode=llm`, then submit a task and confirm a worker-generated
reply. Health is not a provider connectivity test. Check credentials for HTTP 401/403,
base URL/model ID for 404, and JSON response support for 400. Provider response details
are intentionally not exposed to users or logged; validate compatibility separately
without printing keys. A queued message usually requires checking worker liveness
and the shared database before diagnosing the model.

Keep keys out of frontend code, chat, prompts, logs and version control. LLM credentials
come from backend environment configuration, separately from encrypted GitLab/profiler
tokens managed in Project settings. Enabling LLM changes intake and enables profile
interpretation; it does not select a production executor. `PERF_CHAT_EXECUTOR` remains
a separate setting and defaults to `demo`.

TUI commands include `/new`, `/open ID`, `/run`, `/cancel` and
`/set target.repository=...`, `/set target.branch=...`, `/set target.profile=...`.
F2 toggles project settings. A message sent while execution awaits clarification
answers the executor's pending question.

## Validation and maintenance

Run checks appropriate to a change:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
node --test tests/web_state.test.cjs
```

Python tests cover intake, contracts, persistence, execution and TUI behavior. Node
tests exercise the real Web script with controlled asynchronous responses. For UI
layout changes, inspect the page in a browser, including collapsed sidebars.

After changing contract models, regenerate schemas:

```powershell
.\.venv\Scripts\python.exe scripts/export_contracts.py
```

Update `README.md` when setup, behavior or integration changes. `REVIEW.md` records
code review fixes, including the three dynamic-analyzer review iterations. Preserve regression coverage for asynchronous state
handling. This MVP serves a single local user and has no authentication or user
isolation; real provider calls and a production executor require separate validation.

## Credentials and remote GitLab sources

Keep tokens in encrypted backend records, separate from tasks, prompts, tool arguments,
SSE, logs, browser localStorage and draft maps. Blank token inputs preserve values;
explicit Remove clears bindings for future runs. Active runs use immutable credential
references; prune unused old encrypted records on completion/cancellation/failure.
Windows uses DPAPI with machine-scope fallback for service accounts; database ACLs
provide account separation. Other platforms need the secrets extra and a persistent
PERF_CHAT_SECRET_KEY. Fail closed when encryption is unavailable.

GitLab HTTPS sources use integrations/gitlab_source.py and PRIVATE-TOKEN headers.
Profiler MCP uses Bearer headers. Token destinations require HTTPS; HTTP is permitted
only for literal loopback IP addresses or localhost. Validate both when saving tokens
and immediately before generating authentication headers, including old stored tokens.
Bind tokens to their destination and never follow
authenticated redirects. Local aliases keep the existing Git adapter. Preserve pinned
commit/blob identity, bounded responses, source provenance and source/profile uncertainty.


GitLab symbol searches skip unsupported content (binary, LFS pointers, oversized or
non-UTF-8 files). Keep access, transport, timeout and object identity failures fatal;
never treat them as an empty search result. Use the distinct UnsupportedSource error
for content exclusions. GitLab API installations under a URL subpath are not supported.

When changing token handling, run tests/test_credentials.py and the Web state tests;
preserve secure
transport checks, non-echoing validation, active-run token snapshots and cleanup.
Private MCP and GitLab services still require validation against real deployments.

When changing dynamic MCP discovery, planning or analyzer replacement, run
`tests/test_dynamic_analysis.py`, the full Python suite and the Web state tests.
Preserve catalog/schema limits, no-network schema resolution, endpoint/policy pinning,
question/answer context after restart, empty-evidence guards and custom-factory tests.
`docs/analyzer-extension.md` documents the supported lifecycle and its limitations;
update it alongside public analysis contracts and regenerate the exported schemas.
