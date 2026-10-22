# Integration and operations guide

For installation and process management, see [deployment](deployment.md). For
product usage, see the [README](../README.md). This guide owns backend integration
and persistence contracts; extension details live in the linked specialist guides.

## Connection settings and API tokens

Project settings group Repository (provider-neutral URL/reference or local path,
optional branch and Git access token) and Profiler (Result ID, optional API token).
Groups remember their collapsed state independently. Git MCP endpoint and permissions
are configured in ENV. Its optional ENV Bearer token is a fallback; a saved personal
Git token takes precedence, following the profiler authentication flow.
See [Git MCP setup](git-mcp.md) for the complete configuration and security contract.

Git/profiler tokens are encrypted backend records, separate from tasks, prompts, tool
arguments, SSE and logs. Blank preserves the saved token; Remove and Save clears
future bindings. Active runs retain immutable credential references. Unused records
are pruned when their last active run finishes. Unsaved browser tokens are cleared
on chat switches and never retained in drafts/localStorage.

Windows uses DPAPI with machine-scope fallback for service accounts; protect database
ACLs. Other platforms require `.[secrets]` and a persistent `PERF_CHAT_SECRET_KEY`
shared by API and worker. Encryption fails closed. Moving the database to another
machine/account may require re-entering credentials.

Configure `PROFILER_MCP_URL` before saving a profiler token. It is sent only as a
Bearer header to its bound endpoint. Both MCP transports prohibit authenticated
redirects and require HTTPS except localhost/literal loopback HTTP. API responses
expose configured flags only. `PUT /api/conversations/{id}/spec` accepts optional
`credentials.profiler_token` and `credentials.git_token` beside `spec` and
`expected_version`: omission preserves the value; an empty string clears it.
Personal Git tokens are bound to both repository and MCP endpoint. The worker uses
immutable run references and never forwards legacy GitLab token records to Git MCP.
The built-in worker no longer invokes the legacy GitLab REST adapter.

## Git source context

The analyzer receives repository/branch metadata from the run snapshot. With Git MCP,
it discovers allowed tools, forms schema-driven queries and retains generic responses
as `Evidence.server="git"`. No provider-specific API paths or tool names are assumed.
Repository-only analysis can start without a profiler Result ID. Git responses are
not profiling measurements or host-verified source snapshots; repository/commit/build
identity remains unverified unless an adapter establishes it. See [Git MCP](git-mcp.md).

The local Git adapter remains available for explicitly selected local paths or
aliases registered through `PERF_CHAT_GIT_REPOSITORIES`. It never clones/fetches or
changes the working tree. A missing branch defaults to a pinned `HEAD`. Local source
options agreed in chat are stored in `details.profile_analysis.source`, for example:

```json
{"policy":"optional","locations":[{"path":"src/compute.cpp","start_line":10,"end_line":60}],"symbols":[]}
```

`optional` permits analysis when local source is unavailable. `required` requests
an explicit host-verified range; Git MCP output cannot replace it. Required user
ranges are collected independently and prioritized over model-selected excerpts.
`{"policy":"off"}` disables both local reads and Git MCP discovery/calls. To change
a pinned local commit, start a new run. Explicit commits require `scope_repository`
and `scope_ref` matching the accepted run context. Source/profile-build linkage is unverified.

Local preparation outcomes are persisted before analysis and reused after restart;
missing objects never cause substitution with the current branch tip. Reads preserve
whitespace, actual delivered ranges, Git blob hashes and citation IDs. Only regular
UTF-8 blobs are supported: no symlink/submodule traversal, LFS hydration, binary
decoding or code execution. Planner selections use `origin=model_selection`, with
`resolution=model_location` for explicit ranges; symbol matches are only candidates.

Local limits: five queries, five excerpts, eighty lines/8 KiB each, 32 KiB combined,
256 KiB per blob, 1000 search paths, 8 MiB scanned text and twenty hits. Preparation
has a five-second child budget; collection deducts preparation time and reserves
time for reporting within the run deadline. One context builder budgets instructions,
schemas, MCP evidence, documentation answers and source together. Required ranges
cannot silently disappear; narrowing is requested if needed. Optional context may
be omitted. Only actually delivered evidence IDs may be cited.

`PromptContextBuilder` supports an optional provider-specific `token_counter` and
`max_tokens`; the default byte bound is not a token guarantee. Current graph and
replacement contracts are in [analyzer-extension.md](analyzer-extension.md); accepted
repository/ref changes are described in [context updates](analyzer-context.md).

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
$env:PERF_CHAT_MODE = "demo"
$env:PERF_CHAT_EXECUTOR = "profile-demo"
python -m perf_chat.profiler_analysis_agent --result-id demo-1 --goal "Inspect CPU hotspots" --asm
```

This prints a JSON report with **synthetic evidence** using the same dynamic graph
and a deterministic model fixture. All observations are explicitly synthetic. For
standalone analysis, set `PERF_CHAT_EXECUTOR` to `profile-demo` or `profile-mcp`;
the default `demo` selects the separate demonstration executor, not this analyzer.
The legacy `--demo` flag is accepted only when `profile-demo` is already configured;
it cannot override the environment setting. MCP metadata never selects demo mode.
For real LLM planning and interpretation, configure `PERF_CHAT_MODE=llm` and the existing
model settings. To use the real MCP server:

```powershell
python -m pip install -e ".[mcp]"
# Configure in .env for both API and worker:
# PERF_CHAT_EXECUTOR=profile-mcp
# PROFILER_MCP_URL=https://your-server/mcp
# PERF_CHAT_MODE=llm
# LLM_BASE_URL=https://inference.example.com/v1
# LLM_MODEL=your-served-model-id
# LLM_API_KEY=your-provider-key
# PROFILER_MCP_ALLOWED_TOOLS=["your_read_only_tool"]
# PROFILER_MCP_TIMEOUT_SECONDS=60
```

The CLI reads Git MCP ENV settings and supports repository-only analysis with
`--repository`. It does not read saved Web Git/profiler tokens. Use worker/Web for those
encrypted bindings, or inject configured gateways through the Python API.

`PERF_CHAT_EXECUTOR=profile-demo` enables synthetic analysis through the existing Run
button. `demo` keeps the original executor scenario. API and worker must use the same
configuration and be restarted after a change. In profile modes, enter the server's
`result_id` in Profile, describe the task in chat, then Run. Repository and Branch
are not required for profile-only analysis. A supplied repository is optional context;
local source defaults to a pinned HEAD when no branch was specified. Missing repository
access remains an explicit limitation. Required source asks the user to configure a
new run or disable source for the current immutable run. If Profile is empty, a configured Git MCP gateway can analyze the repository;
otherwise execution asks for a result ID. Any additional identifiers or context are requested by the planner
in chat; there is no automatic mandatory metric-selection questionnaire. The answer
resumes the durable run.

If saved metric options are malformed or an analyzer requests metric selection,
the question explains that metrics are profiler counter names. Enter up to six exact
names separated by commas, semicolons or newlines; JSON arrays remain accepted.
Reply `continue`, `I don't know`, `не знаю`, `выбери сам` or `[]` to continue with no
preselected metrics. This does not invent names or prove metric availability; the
analyzer still relies on permitted MCP discovery and evidence. Invalid selections
produce a concrete question with the same no-preselection alternative, rather than
a generic JSON syntax request. Other structured option/source corrections retain
their existing validation rules.

For an invalid top-level analysis options value, `continue` or `{}` accepts defaults.
That acceptance is checkpointed separately from the empty options object, so later
worker steps and restarts do not re-ask solely because the original task was malformed.
The original run task snapshot is retained; corrected options remain in its cursor.

Optional settings agreed in chat are stored in `task.details.profile_analysis`:

```json
{"metrics": ["cpu_samples"], "include_asm": true}
```

The standalone Python API and tool are also available to external execution agents:

```python
from perf_chat.integrations.profiler_mcp import MCPProfilerGateway
from perf_chat.profiler_analysis_agent import AnalysisRequest, AnalysisServices, create_builtin_analyzer
from perf_chat.config import Settings
from perf_chat.llm import ChatModel

settings = Settings.from_env()

agent = create_builtin_analyzer(AnalysisServices(
    profiler=MCPProfilerGateway("https://your-server/mcp", allowed_tools=["your_read_only_tool"]),
    model=ChatModel(settings),
))
report = agent.analyze(AnalysisRequest(result_id="result-7", goal="Inspect CPU hotspots"))
tool = agent.as_tool()  # analyze_profile
```

Load your backend LLM configuration before constructing the model. The adapter is synchronous;
async callers should invoke it in a worker thread. Calls are read-only, timed out and
bounded to a 1000000-character serialized JSON payload. The default 120000-byte model
request budget includes instructions, schemas and context, not just evidence. Optional
context can be omitted; essential evidence that cannot fit yields an explicit limitation.
Full collected evidence remains in the report. No automatic unbounded query loops
are used. The worker persists the full report in `run.cursor.analysis`, available via
`GET /api/runs/{run_id}`. Chat shows excerpts and labels any display truncation.
Conversation snapshots expose only run ID, status, clarification, result and progress;
full task snapshots and raw analysis evidence are available from the run endpoint.
The conversation list returns only IDs and titles (`spec.summary`); use the individual
conversation endpoint to load its history and full current task.
Errors retain collected evidence and do not generate findings from failed calls.
Goal comes from the immutable run task snapshot. Profile/repository/branch use its
initial metadata or explicitly confirmed context updates, never stale copies in
`details.profile_analysis`. See [versioned context updates](analyzer-context.md).
Each new clarification has a distinct, retry-stable ID.
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

The documentation expert uses the same registry and routing protocol as other
experts. LLM routing selects it by description and schema without requiring keywords.
Demo routing matches a keyword or phrase in
`src/perf_chat/profiler_docs_agent/triggers.txt`. Put one literal keyword
or phrase per line. Matching ignores case and requires word boundaries; blank lines
and lines starting with `#` are ignored. The default keyword is `perf`, for example
`How do I collect hotspots with Perf?`. The file is reread each turn without a restart.
An empty, missing or unreadable trigger file disables the demo shortcut.
LLM routing distinguishes consultation, task changes and mixed requests.
`/ask profiler_docs <question>` selects the expert explicitly in either mode.
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
readiness. Web and TUI receive its guidance through the existing intake reply.
Pure consultations preserve the task and do not append intake questions.

## Executor integration contract

`src/perf_chat/contracts.py` is the source of truth. Generate JSON Schema files
in `schemas/` with `python scripts/export_contracts.py`.

- `PerformanceTask`: an immutable run snapshot with `task_id` and `spec_version`.
- `ExecutionEvent`: `progress`, `clarification` or `result`.
- `ClarificationRequest` / `ClarificationAnswer`: correlated through `request_id`.
- `PerformanceResult`: status, measurements with evidence, correctness and artifacts.

`target.branch` selects the branch; `target.profile` stores an opaque result ID/reference
and is never opened as a local file.
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
  repeats the turn. The router receives bounded chat history; TaskAgent excludes
  classified consultations and expert portions of mixed replies. Experts receive
  only their permitted context. History summarization is not implemented.
- Task intake and explicit specification updates increment its version; pure
  consultations preserve it. Existing runs retain their
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

Use the commands and area-specific checks in the
[project validation guide](../AGENTS.md#validation-and-documentation-maintenance).
Tests cover intake, consultations, executor persistence, MCP/source boundaries,
credentials, retries and Web state. Real services require deployment validation.

## Consultation extension

For backend-only expert registration, routing contracts, context isolation and
durable expert questions, see [consultation experts](consultation-experts.md).
This intake extension is independent of ExecutorGateway and custom execution analyzers.
