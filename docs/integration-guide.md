# Integration and operations guide

## Connection settings and API tokens

The right panel has independent collapsible groups: **Repository** (GitLab URL or local
alias, Branch, GitLab API token) and **Profiler** (Result ID, Profiler API token).
Click a group heading or use Enter/Space to toggle it. Each group's expansion state
is remembered in browser localStorage independently of the sidebar.

Token inputs are password fields. Blank keeps the saved token; enter a value to
replace it, or use Remove and Save to clear it for future runs. Unsaved tokens are
cleared when switching chats and are never stored in browser drafts/localStorage.
Metadata and credential changes are saved in one version-checked transaction.

Tokens are encrypted backend records, separate from PerformanceTask, prompts, tool
arguments, SSE and logs. API responses expose configured flags only. Each active run
snapshots opaque credential references; rotation/removal does not change its token.
Unbound old token records are pruned when their last active run finishes or is cancelled.

Windows uses account-scoped DPAPI, falling back to machine-scoped DPAPI for backend
accounts without a loaded user profile. In that case filesystem ACLs provide account
separation; keep the database private. Other platforms require `pip install -e ".[secrets]"`
and a persistent Fernet key in PERF_CHAT_SECRET_KEY shared by API and worker. Missing
encryption support fails the save; there is no plaintext fallback. OS-protected
credentials may need re-entry when moving the database to another machine/account.

Configure PROFILER_MCP_URL on API and worker before saving the profiler token. The
[MCP transport](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/src/mcp/client/streamable_http.py)
sends it as an HTTP Bearer header to that configured endpoint. Select
PERF_CHAT_EXECUTOR=profile-mcp for real profiler data; profile-demo stays synthetic.

GitLab HTTPS URLs need no local clone or alias registration. The read-only API uses
PRIVATE-TOKEN as described in [GitLab authentication](https://docs.gitlab.com/api/rest/authentication/).
The token needs project access and required read API permissions. It is bound to the
exact saved repository URL and is not forwarded to another repository or through
redirects. Local registered aliases continue using local Git objects without tokens.
Remote reads retain commit pinning, blob hashes and source size/type restrictions.
The API must be at the GitLab host-root /api/v4; subpath hosting needs an adapter extension.
Private GitLab/MCP services are not available here; tests simulate their HTTP responses.

PUT /api/conversations/{id}/spec accepts optional `credentials` beside `spec` and
`expected_version`: `{"git_token":"new value","profiler_token":"new value"}`.
Omit a field to retain it; an empty string clears it. TUI shows token status; use
Web or the backend API to manage values. Never paste tokens into chat or repository URLs.

## Git source context

The profile analyzer can enrich metrics with source from a registered **local Git
repository** or an HTTPS GitLab repository. Local reads need no network; GitLab
reads use its API. Neither changes the user's working tree. Repository files and comments are
untrusted evidence, not instructions. The existing analyzer uses a Git gateway and
packaged instructions; there is no additional LLM Git agent.

Register aliases in the backend environment (same configuration for API and worker):

```dotenv
PERF_CHAT_GIT_REPOSITORIES={"my-service":"C:/repos/my-service"}
PERF_CHAT_MODEL_INPUT_BYTES=120000
```

Use `my-service` in Repository and the local branch name in Branch. Configure source
selection through chat in LLM mode. Agreed options are stored in
`details.profile_analysis.source`, for example:

```json
{
  "policy": "optional",
  "locations": [{"path": "src/compute.cpp", "start_line": 10, "end_line": 60}],
  "symbols": []
}
```

For the deterministic demo, after describing the task, send a `field=value` message:

```text
details={"profile_analysis":{"source":{"policy":"optional","locations":[{"path":"src/compute.cpp","start_line":10,"end_line":60}]}}}
```

`optional` preserves metric analysis when source is unavailable. `required` asks for
an explicit source range or selection when model interpretation cannot proceed;
reply with a partial source-options object, or `{"policy":"off"}` to continue
without source. With the model disabled, fragments are retained as evidence and
marked `interpretation_disabled`; no claim of delivery to a model is made.

Symbol search returns text candidates, even if only one hit exists. In required mode
select an explicit path/range from the candidates. Current MCP response formats are
unknown: no profiler source locations are guessed or automatically inferred. The
MVP uses explicitly supplied locations/symbols. Add a format-specific adapter later
if the server exposes reliable source hints.

The worker validates result ID/options, resolves the source revision, persists a
`resolved`, `unavailable` or `disabled` snapshot with a progress event, then analyzes
using that snapshot. Metric/source clarification and worker restart keep the same
commit. Preparation failures are frozen too; retry source resolution in a new run.
Missing pinned objects never cause substitution with the current branch tip.

To select an exact commit through chat, also record `scope_repository` and `scope_ref`
matching current project settings. A stale selection asks for clarification. The
selected commit does **not** prove that it produced the profiled binary; all current
source/profile matches remain `unverified`. A future trusted build manifest can
provide that connection.

The standalone Python API supports source requests and explicit snapshot reuse:

```python
from perf_chat.integrations.git_source import GitSourceGateway
from perf_chat.integrations.profiler_mcp import DemoProfilerGateway
from perf_chat.profiler_analysis_agent import AnalysisRequest, AnalysisServices, create_builtin_analyzer
from perf_chat.profiler_analysis_agent.source_contracts import SourceRequest, SourceLocation
from perf_chat.profiler_analysis_agent.source_context import prepare_source

git = GitSourceGateway({"my-service": "C:/repos/my-service"})
selection = SourceRequest(repository="my-service", ref="main", locations=[
    SourceLocation(path="src/compute.cpp", start_line=10, end_line=60),
])
snapshot = prepare_source(git, selection)  # Persist this for durable standalone retries.
agent = create_builtin_analyzer(AnalysisServices(profiler=DemoProfilerGateway(), source=git))
report = agent.analyze(AnalysisRequest(
    result_id="demo-1", goal="Inspect CPU hotspots", source=selection,
), source_snapshot=snapshot)
```

The CLI accepts `--source-json` with the same `SourceRequest` fields. A configured
model enables interpretation; the demo gateway still produces synthetic metrics.

Source preparation and collection share a five-second child budget per attempt,
within the run deadline. Limits: five snippets, eighty lines each, 8 KiB per snippet,
32 KiB total, 256 KiB per blob, 1000 search paths, 8 MiB scanned text and twenty hits.
Subprocess output is bounded while reading; type/size are checked before loading a
blob. No symlink/gitlink traversal, LFS hydration, binary decoding or code execution.
Paths must be repository-relative; build-machine paths need explicit mapping by the
caller. Only UTF-8 text is currently supported.

One context builder budgets system/schema/goal/MCP/documentation/source together.
Optional sources/docs can be omitted to fit; required ranges request narrowing when
needed. Essential metric evidence that cannot fit produces `insufficient_data`.
`PromptContextBuilder` also accepts an optional provider-specific `token_counter`
and `max_tokens`; the default UTF-8 byte cap is not a universal token guarantee.
Source content preserves whitespace, exact transmitted line ranges, Git blob identity
and SHA-256. Only actually delivered evidence IDs can be cited. Full bounded source
evidence and `delivered_evidence_ids` are retained in the analysis report at the run
endpoint. Existing reports without source fields remain valid.

The design and four planning reviews are recorded in
[Source context design](source-context-plan.md).

## Dynamic profile analyzer

There is one built-in implementation, `DynamicProfilerAnalyzer`, for real and synthetic
profiler sessions. See [the analyzer extension guide](analyzer-extension.md). The former
fixed graph and three-method protocol have been removed; no real tool name or response
catalog format is prescribed by the application.

```mermaid
flowchart TD
    discover --> plan
    plan -->|Permitted profiler query| call
    plan -->|Read pinned repository evidence| source
    plan -->|Missing context| clarification
    clarification --> plan
    call --> plan
    source --> plan
    plan -->|Usable evidence collected| report
    report --> format_report
```

Configure the MCP endpoint and explicitly permitted read-only functions. The analyzer
receives the opaque Result ID, task goal and optional repository/revision metadata.
Authentication tokens stay in transport headers. Tool names and argument shapes come
from discovery; only allowed functions can execute. The planner chooses investigation
order, forms arguments using schemas and known context, and asks focused questions for
missing values. The host validates schemas, bounds work and pins endpoint/tool policy.

Repository access is optional, bounded and read-only. An explicit local Git path may
be used directly; aliases remain registered through backend settings. Source reads
use a frozen revision (local default `HEAD`) and preserve provenance. The planner may
select file ranges or symbol candidates; source text does not verify profile/build
linkage. There are no clone/fetch, code execution or arbitrary filesystem-read tools.
Reports pass through a durable Markdown formatting node for Web/TUI. Chat contains
bounded findings, limitations and evidence references; unvalidated model text is labeled
as such and does not become accepted observations. Full output stays in run details.
Reports separate observations/hypotheses and retain raw profiler evidence, source
provenance, timestamps and hashes. Private services still need deployment validation.

To run the graph without a server or model:

```powershell
python -m perf_chat.profiler_analysis_agent --demo --result-id demo-1 --goal "Inspect CPU hotspots" --asm
```

This prints a JSON report with **synthetic evidence** using the same dynamic graph
and a deterministic model fixture. All observations are explicitly synthetic. To enable interpretation, configure `PERF_CHAT_MODE=llm`
and the existing model settings. To use the real MCP server:

```powershell
python -m pip install -e ".[mcp]"
# Configure in .env for both API and worker:
# PERF_CHAT_EXECUTOR=profile-mcp
# PROFILER_MCP_URL=https://your-server/mcp
# PROFILER_MCP_TIMEOUT_SECONDS=60
```

`PERF_CHAT_EXECUTOR=profile-demo` enables synthetic analysis through the existing Run
button. `demo` keeps the original executor scenario. API and worker must use the same
configuration and be restarted after a change. In profile modes, enter the server's
`result_id` in Profile, describe the task in chat, then Run. Repository and Branch
are not required for profile-only analysis. A supplied repository is optional context;
local source defaults to a pinned HEAD when no branch was specified. Missing repository
access remains an explicit limitation. Required source asks the user to configure a
new run or disable source for the current immutable run. If Profile is empty, execution asks
for a result ID. Any additional identifiers or context are requested by the planner
in chat; there is no automatic mandatory metric-selection questionnaire. The answer
resumes the durable run.

Optional settings agreed in chat are stored in `task.details.profile_analysis`:

```json
{"metrics": ["cpu_samples"], "include_asm": true}
```

The standalone Python API and tool are also available to external execution agents:

```python
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent import AnalysisRequest, AnalysisServices, create_builtin_analyzer

agent = create_builtin_analyzer(AnalysisServices(
    profiler=MCPProfilerGateway("https://your-server/mcp", allowed_tools=["your_read_only_tool"]),
    model=ChatModel(settings),
))
report = agent.analyze(AnalysisRequest(result_id="result-7", goal="Inspect CPU hotspots"))
tool = agent.as_tool()  # analyze_profile
```

Import `ChatModel` and initialize `settings` with your backend LLM configuration. The adapter is synchronous;
async callers should invoke it in a worker thread. Calls are read-only, timed out and
bounded to 1 MB per decoded payload. Model interpretation is skipped above a 120 KB
evidence budget; full data remains in the report. No automatic unbounded query loops
are used. The worker persists the full report in `run.cursor.analysis`, available via
`GET /api/runs/{run_id}`. Chat shows excerpts and labels any display truncation.
Conversation snapshots expose only run ID, status, clarification, result and progress;
full task snapshots and raw analysis evidence are available from the run endpoint.
The conversation list returns only IDs and titles (`spec.summary`); use the individual
conversation endpoint to load its history and full current task.
Errors retain collected evidence and do not generate findings from failed calls.
Profile and goal come from the current task snapshot, even if stale copies exist in
`details.profile_analysis`. Each new clarification has a distinct, retry-stable ID.
Analysis calls share the remaining run deadline: MCP and model timeouts are clamped,
and no subsequent call starts after expiry. Model calls use a total asynchronous
timeout as well as HTTP phase timeouts. Both configured timeouts must be finite,
positive and at most 120 seconds. Synchronous custom gateway calls must
also enforce their own transport timeout; late completion is rejected by the worker.
Malformed analysis options and invalid result-ID answers produce a new clarification
instead of terminating the run. Unknown keys in analysis options are ignored; supported
keys are `metrics`, `include_asm` and optional `source`.
`completed` means the analysis pass completed, not that optimization succeeded.
References are checked structurally; factual interpretation still requires review.
The private MCP server and real model have not been exercised in this workspace.

## Profiler documentation expert

The knowledge base is `src/perf_chat/profiler_docs_agent/Perf.md`. It is intentionally
empty. Replace its contents with UTF-8 Markdown documentation for your profiler, including versions and
platforms. The file is included in installed packages and read again for each lookup.

The intake graph calls the expert when the latest user message matches a keyword
or phrase in `src/perf_chat/profiler_docs_agent/triggers.txt`. Put one literal keyword
or phrase per line. Matching ignores case and requires word boundaries; blank lines
and lines starting with `#` are ignored. The default keyword is `perf`, for example
`How do I collect hotspots with Perf?`. The file is reread each turn without a restart.
An empty, missing or unreadable trigger file disables automatic chat lookups.
These triggers do not invoke MCP tools or control documentation lookup during report
generation.
In LLM mode the expert receives the complete UTF-8 document, the current question and
optional version/platform. It receives no conversation history, task, metrics or source
code and has no execution/browser tools. Other agents receive only its concise answer
and cited source metadata. There is no lexical retrieval or embedding index.

The first content line after a heading may be a standalone Markdown link with any label,
for example `[Manual](https://docs.example.com/profiler/cpu)`. That destination is the
section source; subsections inherit it unless they define another. Links inside fenced
code are not sources. Only HTTP(S) URLs without embedded credentials are accepted;
sections without one are labeled as local documentation. The model selects source IDs
from a registry, and the application renders section-title links. Unknown IDs, unregistered
Markdown link targets and legacy line citations are rejected. Document hashes identify
the snapshot; line numbers are not used for citations. Links are never fetched.

`PERF_CHAT_DOCS_MAX_INPUT_BYTES` defaults to 2000000 and limits the complete serialized
expert request, including instructions, document and source registry. Oversized input
returns `too_large` without truncation or a model call. This byte bound does not count
tokens or guarantee provider context capacity; reserve room for the model response and
configure the model/budget accordingly. The model-generated answer text is bounded to
16000 characters and 64 cited IDs; rendered source labels and URLs are appended separately. Its timeout shares the run deadline. The analyzer retains its separate
model-input budget for metrics, the expert answer and source context.

Empty, missing or unreadable files produce explicit limitations without model calls.
If the complete document does not answer a question, the expert returns `not_found`.
Demo mode reports that synthesis is unavailable and does not echo the document.
`found` does not prove factual correctness or platform compatibility. Documentation is
untrusted reference data, not agent instructions.

External execution agents can reuse it directly or as a LangChain/LangGraph tool:

```python
from perf_chat.profiler_docs_agent import ProfilerDocsAgent

expert = ProfilerDocsAgent()  # Pass ChatModel(settings) to enable synthesis.
answer = expert.ask("Perf hotspots collection", version="2026", platform="Linux")
tool = expert.as_tool()  # ask_perf_docs; bind to an external execution agent.
```

The expert does not execute Perf, read profiles, infer measurements or change launch
readiness. Web and TUI receive its guidance through the existing orchestrator reply.

## Overview

This local MVP shares one API between Web and TUI clients. Two LangGraph agents
build a performance task specification. Execution is delegated through
`ExecutorGateway`. The default executor is a **demo** and performs no real optimization.

## Structure

```text
src/perf_chat/
  ui/                    # HTTP clients only
    web/                 # HTML, CSS and JavaScript without a build step
    tui.py               # Textual terminal interface
    client.py            # TUI HTTP/SSE client
  orchestrator/
    agent.py             # User-facing conversation
    graph.py             # LangGraph: specification -> validation -> reply
  task_agent/
    agent.py             # Structured PerformanceTask
  contracts.py           # Versioned integration boundary
  executor.py            # Demonstration ExecutorGateway
  api.py                 # FastAPI, REST and SSE
  worker.py              # Queue processing outside HTTP requests
  store.py               # SQLite conversations, versions, jobs, runs and events
  llm.py                 # OpenAI-compatible Chat Completions
  config.py
```

## Installation and startup

For platform-specific commands and listener configuration, see
[the deployment guide](deployment.md).

Requires Python 3.11+. Run commands **from this project directory**.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

To reproduce the validated environment, run
`python -m pip install -r requirements.lock.txt` inside the virtual environment
before the editable installation. The lock file was generated on Windows with Python 3.12.

Use separate terminals with the same `.env` and working directory:

```powershell
# Terminal 1: API and Web
.\.venv\Scripts\python.exe -m perf_chat.api

# Terminal 2: agents and executor
.\.venv\Scripts\python.exe -m perf_chat.worker

# Terminal 3: optional TUI
.\.venv\Scripts\python.exe -m perf_chat.ui.tui
```

Web: <http://127.0.0.1:8000>. OpenAPI: <http://127.0.0.1:8000/docs>.
The TUI opens the latest conversation; use `--conversation ID` to select one explicitly.
Web and TUI share conversations. The API listens on loopback only. This MVP serves
one local user; authentication and user isolation are not implemented.

## Agent modes

`PERF_CHAT_MODE=demo` makes no model calls. It saves the task description from chat;
meaningful clarification requires LLM mode. Demo mode tests integration rather than
natural-language understanding. Messages also support `field=value` for compatibility.

The right sidebar collapses to a narrow rail labeled "Project settings". Click the
rail to expand it; use its header button or Esc to collapse it. It starts collapsed
and remembers its state. The left "Chats" sidebar behaves the same way and remembers
its state independently. Repository, Branch and Profile provide task context; connection
settings and tokens are described above. In profile analysis modes, Profile supplies
the MCP `result_id`; the original demo only stores the reference. Discuss the task,
acceptance criteria and constraints in chat. Run and Cancel remain available in the
header when the sidebar is hidden.

`PERF_CHAT_MODE=llm` enables both intake agents through an OpenAI-compatible API:

```dotenv
PERF_CHAT_MODE=llm
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-model
LLM_API_KEY=your-key
```

The provider must support `/chat/completions` and
`response_format={"type":"json_object"}`. The schema is included in the prompt;
Pydantic validates the output. An invalid response produces `job.failed` without
changing the specification. The key stays in the backend/worker environment.
Real provider calls require your configuration and have not been separately validated.
LLM mode enables conversational intake and profile interpretation. The demo executor
remains a demo until you connect the external execution infrastructure. Executor
selection is configured separately as described above.

## Demo

Enter Repository and Branch in "Project settings" and save. Profile can be empty.
Then send a chat message, for example:

```text
Reduce allocations when loading a large file while preserving behavior.
Validate the result by comparing profiles for the same scenario.
```

Click "Run". The demo executor asks about the environment; reply with a normal
message. The result has `status=demo`, without fabricated measurements.
In LLM mode, the orchestrator asks questions relevant to the specific task.
A numeric metric, baseline and benchmark are not universally required fields.

TUI commands include `/run`, `/cancel`, `/new`, `/open ID`,
`/set target.branch=feature/perf`, `/set target.repository=local/repo` and
`/set target.profile=profile-id-7`. F2 toggles the context panel.
A normal message while execution awaits clarification answers the executor's question.

## Executor integration contract

`src/perf_chat/contracts.py` is the source of truth. Generate JSON Schema files
in `schemas/` with `python scripts/export_contracts.py`.

- `PerformanceTask`: an immutable run snapshot with `task_id` and `spec_version`.
- `ExecutionEvent`: `progress`, `clarification` or `result`.
- `ClarificationRequest` / `ClarificationAnswer`: correlated through `request_id`.
- `PerformanceResult`: status, measurements with evidence, correctness and artifacts.

`target.branch` selects the branch; `target.profile` stores an opaque path or ID.
`summary` holds the task description, `details` holds agreed parameters, and
`open_questions` holds material unresolved questions. Launch requires either a repository or a profile,
a nonempty summary and no open questions. A branch is needed only when resolving
source; profile-only analysis does not require one.
Legacy typed fields remain optional for compatibility and facts extracted from chat.
Legacy `target.revision` is read from saved snapshots but is not displayed and does
not replace branch: a commit hash is not accepted as a branch name.

Implement `ExecutorGateway.step(run_id, task, cursor, answer)` and `cancel(run_id)`.
Pass the adapter to `Worker(settings, executor=YourGateway(...))` in the worker
entry point. UI and API changes are unnecessary. A gateway can call an external
service over HTTP or invoke a graph in the same process.

`step` returns one event and a JSON-serializable cursor. A question preserves the
cursor until the answer arrives; the next call receives `ClarificationAnswer`.
The external execution infrastructure manages subgraph state and maps `run_id` to
LangGraph `thread_id`. If it uses `interrupt`, the gateway converts it to
`ClarificationRequest` and the user's answer to `Command(resume=...)`.

Jobs use **at-least-once delivery**: a step may repeat after a crash. The adapter
must deduplicate side effects by `run_id` and cursor, persist graph checkpoints,
and return previously computed results on retry. Adapter calls must have timeouts.
The job lease lasts ten minutes. A stale worker cannot overwrite the result after
another worker acquires the lease.

## State and MVP limitations

- SQLite WAL stores conversations, jobs, results and events. Do not place this
  database on a network filesystem. Store can be replaced with PostgreSQL as load
  grows while preserving contracts and the client API.
- HTTP only queues jobs. The worker continues after clients close. A job interrupted
  by a crash becomes available for another worker after ten minutes.
- The intake graph completes each turn as a unit. Inputs and results are persisted
  at the application level; there are no checkpoints within an LLM turn, so a crash
  repeats the turn. The model receives full history; summarization is not implemented.
- Updating the specification increments its version. Existing runs retain their
  original snapshot. Each conversation allows one active run; its task can still be edited.
- SSE replays events using `Last-Event-ID` or `after`. Events and state changes are
  atomic. The stream reports stages rather than individual LLM tokens.
- Clients retry snapshot loading after temporary failures, even if the latest event
  was already delivered. Conversation snapshots include progress and the latest error.
- The intake queue records the history boundary when accepting a message so a late
  executor reply cannot replace a newer task. Partial LLM JSON preserves omitted
  fields in the current specification; explicit values are validated by Pydantic.
- Web preserves message and settings drafts when switching conversations in the
  current tab. Reloading the page discards drafts. Settings are rebased onto the
  latest task version; conflicting changes from another client require explicitly
  loading current values using the sidebar button.
- Cancellation is cooperative: the current gateway call must return before `cancel`
  is called. The time budget is checked between steps; waiting for user input does
  not consume it. The executor owns real optimization iterations and hard timeouts;
  the local worker also limits execution to 100 steps.
- Provider failures are not automatically retried. An error event is saved and the
  user can resend the message. Intake commands and run creation are deduplicated
  by `request_id`.
- The context form and TUI `/set` command only edit repository, branch and profile.
  Other details are clarified in chat. Saving checks `expected_version`.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
node --test tests/web_state.test.cjs
```

Tests cover the intake graph, the LLM adapter with mock transport, versions, run
snapshots, clarifications, recovery, deduplication, cancellation and headless TUI
startup. JavaScript tests require Node.js 18+; Web still needs no build step.

Profiler API tokens require an HTTPS MCP URL; HTTP is allowed only for localhost or
literal loopback addresses. This is checked when saving and sending tokens. GitLab
symbol searches skip binary, oversized, LFS and non-UTF-8 content while preserving
access and integrity errors.
