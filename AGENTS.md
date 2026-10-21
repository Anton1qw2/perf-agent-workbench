# Project guide

## Purpose and boundaries

**chats** is a local MVP for defining performance optimization tasks through chat
and handing them to external execution infrastructure. Python package: `perf_chat`;
distribution: `perf-chat`. Web and TUI share an API, history and task state.
Two LangGraph intake agents handle user communication and structured task extraction.
A bounded intake router selects task changes or backend consultation experts first.
The built-in profiler analyzer investigates evidence; external optimization agents
connect through `ExecutorGateway`.

This MVP serves one local user without authentication or user isolation. Synthetic
output never establishes real measurements, completed optimization or improvement.
Private MCP and LLM deployments require separate compatibility validation.

## Code map and ownership

Paths are relative to the repository root.

| Path | Responsibility |
| --- | --- |
| `src/perf_chat/ui/web/` | Vanilla HTML/CSS/JavaScript; no build step |
| `src/perf_chat/ui/tui.py`, `ui/client.py` | Textual UI and HTTP/SSE client |
| `src/perf_chat/orchestrator/` | User-facing agent and intake graph |
| `src/perf_chat/experts/` | Common consultation registry, context policy and default expert registrations |
| `src/perf_chat/task_agent/` | Structured task extraction and partial updates |
| `src/perf_chat/profiler_docs_agent/` | Documentation expert, full-document loader, `Perf.md`, `triggers.txt` |
| `src/perf_chat/profiler_analysis_agent/` | Dynamic graph, analysis/source contracts, context, reporting and executor bridge |
| `src/perf_chat/integrations/` | Profiler/Git MCP discovery and calls, JSON Schema validation, local Git access |
| `src/perf_chat/contracts.py` | Task, run, event and executor boundary models |
| `src/perf_chat/api.py`, `worker.py`, `store.py` | HTTP/SSE, durable jobs and SQLite persistence |
| `src/perf_chat/executor.py` | Demonstration executor |
| `src/perf_chat/config.py`, `llm.py`, `runtime.py` | Settings, Chat Completions adapter and deadlines |
| `src/perf_chat/credentials.py` | Backend token encryption |
| `schemas/`, `scripts/export_contracts.py` | Generated schemas and their exporter |
| `tests/` | Python regression tests and Node Web state tests |

Keep UI, orchestrator and task extraction separate. Clients use HTTP/SSE; they must
not invoke agents or access SQLite. The API queues work; the worker executes it.
Task/executor models live in `src/perf_chat/contracts.py`; analyzer and source models
live in `contracts.py` and `source_contracts.py` inside
`src/perf_chat/profiler_analysis_agent/`.
Edit models, then regenerate schemas; do not maintain generated schemas independently.

## Task intake and UI

- Documentation, comments, test examples, labels, built-in messages and prompts use
  English. Preserve compatibility with saved optional contract fields.
- Store the task description in `summary`, agreed parameters in `details`, and
  material unresolved questions in `open_questions`. Partial LLM updates must not
  erase agreed fields.
- Launch requires a nonempty summary, no open questions, and either a repository
  or a profile. Do not require both, a universal metrics questionnaire, or a branch
  for profile-only analysis. Request missing execution data in chat.
- Profile is an opaque MCP `result_id`, never a local file path. Repository-only
  launches need no result ID when Git MCP is configured. Otherwise the profile
  executor requests it. Ask for a branch only when source
  resolution needs it.
- Preserve the compact blue design and shared composer with Send at bottom right.
  Sidebars collapse to clickable rails, remember state independently and become
  overlays on narrow screens. Repository and Profiler settings collapse separately.
- Keep the execution status panel two lines high with scrolling; Copy copies all
  text. Use calm red for errors, green for completion and blue for active work.
  Animate only active work; clarification/reconnection stay static. Respect reduced
  motion. A completion badge does not prove a performance improvement.
- Preserve per-conversation drafts, protection against late HTTP responses, SSE
  connection ownership and snapshot retries. A late snapshot must not clear an SSE
  disconnect notice before reconnection.
- Keep Web/TUI Markdown compatible. Escape raw HTML and reject unsafe links; Web
  must not load remote images from chat messages.

## Consultation experts

Intake routes each message as task, consultation or mixed before TaskAgent.
Pure consultations must preserve the complete task and spec_version, need no
repository/profile and must not append task-readiness questions or Run suggestions.
Mixed routing requires an explicit user request to change the task. Routing failures
leave the task unchanged. `/ask <expert_id> <question>` explicitly selects an expert;
`/task <text>` explicitly resumes task intake. No UI registration or code loading.

Register trusted factories through ExpertRegistry and Worker(..., experts=...).
An explicit registry replaces defaults; start from default_experts() to retain
profiler_docs. Definitions declare input schemas and ContextPolicy (task/history
denied by default). Descriptions do not authorize tools. ExpertServices supplies
the model; additional read-only services are explicit factory dependencies.
Keep tokens transport-only. Experts are trusted Python code, not sandboxed plugins.
Validate custom inputs once and pass a deep copy of the typed model. Use JSON dumps
for size checks, never as a second validation input: aliases, AliasPath and computed
fields do not necessarily round-trip through model_dump().

One expert may be called per turn. The Worker intake deadline is 120 seconds;
routing, extraction, expert services and response generation share it. Custom
synchronous experts must bound their transports; reject late answers. Routing
requests and expert inputs/context/results use PERF_CHAT_MODEL_INPUT_BYTES.
The registry allows at most 32 experts. ExpertResponse bounds answers to 16000
characters, sources to 64, and limitations to 16 entries of 2000 characters.
These are code-owned limits, not new environment variables.

Store owns internal intake_turns records: route, result, pending expert question
and consultation message indices commit atomically with job completion and reply.
Check lease ownership before writing. Default missing records to no pending expert.
Exclude classified consultation messages from later TaskAgent input while retaining
them for routing and chat display. Do not put expert state into PerformanceTask.
Expert needs_input questions survive Worker restart and never become execution
ClarificationRequests. Existing API execution clarification priority is preserved.
Retain pending expert questions on unavailable/routing failures; clear them on
completion or explicit conversation switching. Check the intake deadline again
after the graph returns, before committing any task changes or assistant reply.
For an expired pure consultation, discard all generated output and new questions;
commit only a host-owned unavailable reply, classification/message indices and the
previous pending question only for a continuing consultation, under the job lease.
Use the same retain_pending rule in normal routing and expiry fallback: explicit
/ask or a different selected expert clears the previous question even on timeout.
This keeps failed consultations out
of later task extraction. Expired task/mixed results must never update the task.

ProfilerDocsAgent directly accepts ExpertRequest and returns the common ExpertResponse;
there is no documentation-specific graph path or adapter. Its original
ask(question, version, platform)/as_tool() APIs and analysis lookups remain compatible.
Its default context policy and document synthesis use only question/version/platform.
All experts appear in the LLM routing catalog; matchers are demo shortcuts only.
Guidance, sources and
user answers are not measured evidence or task agreement. See
[consultation experts](docs/consultation-experts.md) for the extension contract.

## Durable execution

`ExecutorGateway.step(run_id, task, cursor, answer)` returns an event and a
JSON-serializable cursor; `cancel(run_id)` handles cancellation. Supply the gateway
to `Worker`. The integration owns graph state and LangGraph thread mappings.
Task versions become run snapshots; clarification replies use `request_id`.

SQLite WAL stores jobs and events. Jobs have leases and may be redelivered after
failure; tolerate retries before a checkpoint transaction commits. Distinguish a
step awaiting clarification from its resume with an answer. Cancellation is
cooperative. Only one run may be active per conversation. Preserve optimistic
version checks, request deduplication and atomic state/event updates.

API and worker must agree on `PERF_CHAT_EXECUTOR`:

| Value | Behavior |
| --- | --- |
| `demo` | Default synthetic execution scenario |
| `profile-demo` | Dynamic graph with synthetic MCP data; deterministic model fixture when LLM mode is off |
| `profile-mcp` | Real read-only profiler and/or Git MCP queries with bounded LLM planning |

`PERF_CHAT_MODE` controls model use separately; it does not select an executor.
Profiler, documentation and model calls share the run deadline. Reject late success
and enforce total call timeouts. Use retry-stable clarification IDs, distinct for
each new question. Preserve corrected analysis options across questions; current
run task summary and accepted profile context override stale copies in free-form details.

## Dynamic profiler analysis

The only built-in implementation is `DynamicProfilerAnalyzer` in `dynamic.py`.
The fixed analyzer and its three-method profiler protocol were removed. Each durable
worker step executes one graph node:

```text
discover -> plan -> call/source -> plan -> ... -> report -> format_report
              |
              +-> clarification -> resume
              +-> context proposal -> confirmation -> commit -> refresh -> plan
```

The analyzer receives an optional result ID, goal and optional repository/branch. It chooses
profiler queries, bounded source reads, clarification or completion. Preserve these
invariants when extending or replacing it:

- Configure the endpoint and `PROFILER_MCP_ALLOWED_TOOLS`; the allowlist defaults to
  empty. Discovery, descriptions and annotations do not authorize calls. Allow only
  read-only operations because calls may be redelivered.
- Git uses `GIT_MCP_URL`, `GIT_MCP_ALLOWED_TOOLS`, `GIT_MCP_TIMEOUT_SECONDS` and
  optional backend-only `GIT_MCP_TOKEN`. It is provider-neutral: no GitLab/GitHub API
  paths or tool-name assumptions. Factories receive `AnalysisServices.git`.
  Decisions namespace calls with `server=profiler|git`; catalogs/fingerprints are
  pinned separately, while call/planning budgets are shared. Git evidence carries
  `server="git"` and reports retain `git_tool_catalog`. Empty allowlists deny calls.
  `profile-demo` never connects to real Git MCP. Built-in identity is v4; v3 runs restart.
- Context updates use strict proposals for Result ID/repository/branch, explicit
  correlated yes/no confirmation and a host-owned ledger (at most eight updates).
  Preserve the original run task. Acceptance is a separate durable step before
  source/discovery/model work. A Git-only run may add a profile after confirmation;
  preserve prior evidence and scope rather than restarting or rewriting the task.
  See `docs/analyzer-context.md` for lifecycle, null semantics and scope rules.
- Custom analyzers receive the new `context_updates` keyword only when they declare
  `supports_context_updates=True`. Questions/normalization stay in the analyzer;
  Worker owns confirmation, validation, persistence and credential boundaries.
  Confirmation replies must never enter the analyzer question/answer journal.
- Refresh catalogs without resetting evidence, questions, counters or deadlines.
  Compare every performed discovery against pinned definitions, including `[]`;
  distinguish inactive services from empty actual catalogs. Tag evidence with its
  immutable context. Replace excludes old profiles from active model input; compare
  explicitly retains profiles in the same repository/ref scope. Preserve full audit
  history and validate citations only against delivered active evidence.
- Build arguments from discovered schemas, task context, evidence and answers.
  Do not hardcode profiler tool names or require an argument map for new servers.
  `include_asm` is a planning preference, not name-based authorization.
- Validate arguments before checkpointing a call; schema mismatches return bounded
  feedback without execution or echoing values. Revalidate in the gateway. Validate
  input/output schemas without network retrieval and reject changed tool definitions.
- `PROFILER_MCP_PROFILE_ARGUMENTS` is an optional legacy constraint, default `{}`.
  Without it, schema validation does not establish profile identity; retain that
  limitation rather than claiming a verified association.
- `ProfileAnalyzer.step_analysis` / `AnalysisStep` is the replacement boundary.
  Inject `Worker(..., analyzer_factory=...)`; factories receive per-run
  `AnalysisServices`. Restore JSON checkpoints or a persistent external checkpointer;
  do not rely on an in-memory graph surviving between worker steps.
- Retain `analyzer_id` and `analyzer_state` in the cursor, including catalog,
  decisions, evidence and the gateway configuration fingerprint. Pin endpoint,
  allowlist and legacy argument policy. Fail closed on drift, including between
  planning and execution. Do not switch implementations during a run; removed-analyzer
  checkpoints require a new run. Keep saved reports readable.
- Journal every issued analysis-input question, including host-generated ones.
  Pass ordered question/answer pairs to planning and interpretation within the
  shared model input budget. Answers are context, not measured evidence.
- Require usable evidence before interpretation, including on budget exhaustion.
  Null, empty arrays/objects and whitespace-only text are insufficient. Preserve
  zero/false values without inventing their semantics.
- Preserve server values and units, separate observations from hypotheses and
  validate evidence citations. Do not add a calculations module or invent missing
  metric semantics. Tool catalogs alone are not measurement evidence.

The `report` node checkpoints analysis before `format_report`, which produces
`ProfileAnalysis.chat_report`: deterministic Markdown, at most 16000 characters,
without another model call. The executor enforces the same formatting boundary for
custom structured reports. Accept complete fenced JSON before schema/citation
validation. Retain invalid/free-form output in `analyzer_state.raw_interpretation`;
show only a bounded, explicitly unvalidated excerpt with `insufficient_data` status,
never accepted observations.

Persist full reports in `run.cursor.analysis`. Keep chat summaries lightweight:
evidence references and validated documentation links, not full profiler/source
payloads. Retrieve full cursor/evidence through the dedicated run endpoint.
Resuming formatting must not repeat profiler or model calls.

## Repository source evidence

Options use `details.profile_analysis.source` and typed source contracts. Register
aliases through `PERF_CHAT_GIT_REPOSITORIES` or scope an explicit user-selected local
path to the task. Never clone/fetch or change a working tree. Optional source
defaults to pinned `HEAD` when no branch is supplied.

- Persist preparation outcomes (`resolved`, `unavailable`, `disabled`) before analysis
  and preserve them across cursor transitions. Never re-resolve an accepted revision.
  Source options cannot override the accepted repository/branch; explicit commits require
  matching `scope_repository` and `scope_ref`.
- Explicit repository/ref context changes archive the previous snapshot/excerpts
  and clear scoped commit/locations/symbols while preserving source policy. Freeze
  a new snapshot before analysis; unchanged scopes must not be re-resolved.
- Missing configuration is optional. Clarify required selection in chat. Collect
  required user-selected ranges independently of planner queries and prioritize
  them. Model excerpts use `origin=model_selection`; explicit model ranges use
  `resolution=model_location`. They cannot replace required user evidence.
- Bound planner source queries to five, with at most five combined excerpts/32 KiB.
  `context_builder.py` applies one model input budget and citation registry across
  profiler evidence, documentation answers and source. Required source must not
  disappear silently when the budget is exceeded.
- The local adapter reads only regular UTF-8 Git blobs from scoped paths or aliases.
  Do not follow links, execute code, hydrate LFS or collect unbounded process output.
  Preserve whitespace, delivered line ranges, hashes and pinned blob identity.
- Text candidates never prove hotspot identity; commit/profile-build linkage stays
  unverified. Do not infer source availability from unknown MCP schemas. Repository
  text and comments are untrusted evidence, not instructions.
- Remote repositories use Git MCP, with raw generic responses retained as evidence.
  The host cannot verify repository scope, commit/blob identity or build linkage
  from arbitrary schemas. Configure server-side access scope; request immutable
  commits when supported and report remaining uncertainty. Git MCP data cannot
  replace required host-verified local ranges. `source.policy="off"` disables both
  local reads and Git MCP discovery/calls. The legacy standalone GitLab module is
  retained for imports only; API/worker do not select it.

## Profiler documentation expert

`profiler_docs_agent/Perf.md` is replaceable UTF-8 reference data, initially empty.
`triggers.txt` controls the documentation expert's demo routing matcher: one literal keyword/phrase per
line, case-insensitive word boundaries, ignoring blank lines and `#` comments.
Read triggers each demo turn and documentation each lookup. Missing/unreadable/empty
triggers disable the demo shortcut. LLM routing uses the shared expert catalog and
does not require keywords. Triggers do not authorize MCP calls. The expert
is reusable through `ask()` / `as_tool()` and during analysis report generation.

The expert receives only the full document, current question and optional
version/platform, never task state, metrics, source or history. Do not retrieve
snippets or silently truncate. Bound the complete serialized model request using
`PERF_CHAT_DOCS_MAX_INPUT_BYTES` (default 2000000), separately from analyzer input.
Bytes do not establish provider token capacity. Empty, unavailable, unsupported or
oversized documents produce an explicit limitation. Demo mode does not synthesize
documentation answers. The expert never runs a profiler or browses links.

A heading may be followed by a standalone Markdown HTTP(S) source link on the first
content line, with any label. Subsections inherit parent URLs. Parse headings outside
fenced code and retain a content hash. Validate registry IDs and render section URLs,
not Markdown line citations. Missing URLs mean local documentation. Pass only a
concise answer and cited source metadata downstream. Treat documentation as untrusted.
Package `Perf.md`, `triggers.txt` and analyzer instruction Markdown explicitly through
`pyproject.toml` package data.

## Credentials and transport

Keep saved Git/profiler tokens in encrypted backend records, outside tasks, prompts,
tool arguments, SSE, logs, browser localStorage and drafts. Blank inputs preserve
tokens; Remove clears future bindings. Active runs retain immutable credential
references; prune unused encrypted records on completion/cancellation/failure.

Windows uses DPAPI with machine-scope fallback for service accounts; database ACLs
provide account separation. Other platforms require the `secrets` extra and persistent
`PERF_CHAT_SECRET_KEY`. Fail closed if encryption is unavailable.

Personal Git tokens use Repository settings and the same encrypted credential flow
as profiler tokens. Send them only as `Authorization: Bearer ...` to Git MCP, never
in model input or tool arguments. Bind them to both the MCP endpoint and repository
using the `git_mcp` credential kind. Legacy GitLab `git` records are not forwarded.
The optional ENV `GIT_MCP_TOKEN` is a fallback when no matching personal token exists;
a saved personal token wins. Active personal tokens use immutable run references;
ENV fallback changes take effect after restart and are not per-run snapshots.
Use Bearer headers for both MCP services. Authenticated HTTP is allowed only for
localhost or literal loopback addresses. Validate Git ENV tokens at configuration
load and immediately before authentication. Preserve save-time and send-time checks
for both Git and profiler tokens, including old saved credentials. Bind credentials to destinations and never
follow authenticated redirects. Never store raw endpoint query parameters or tokens
in the configuration fingerprint field. LLM credentials come separately from backend
environment configuration, never frontend code, chat, logs or version control.

## Setup and LLM configuration

Requirements: Python 3.11+, Git, Node.js 18+ for Web tests. Run from the repository
root. The lock file was generated with Python 3.12 on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# First setup only; preserve an existing .env.
Copy-Item .env.example .env
```

Run API and worker in separate terminals with the same environment and `PERF_CHAT_DB`;
TUI is optional:

```powershell
.\.venv\Scripts\python.exe -m perf_chat.api
.\.venv\Scripts\python.exe -m perf_chat.worker
.\.venv\Scripts\python.exe -m perf_chat.ui.tui
```

Default API/Web: `http://127.0.0.1:8000`. `PERF_CHAT_API_HOST` / `PERF_CHAT_API_PORT`
control the listener; `PERF_CHAT_API_URL` is the TUI destination. Install `.[mcp]`
for real MCP access. `PERF_CHAT_MODE=demo` needs no model credentials.
For LLM mode, configure the backend `.env` or process environment:

```dotenv
PERF_CHAT_MODE=llm
LLM_BASE_URL=https://inference.example.com/v1
LLM_MODEL=your-served-model-id
LLM_API_KEY=your-provider-key
LLM_TIMEOUT_SECONDS=90
```

The adapter posts non-streaming requests to `{LLM_BASE_URL}/chat/completions`.
Include the API prefix, not that suffix. Use the server's exact model ID. The server
must accept system/user messages and `response_format={"type":"json_object"}`, and
return nonempty `choices[0].message.content` text. Native, streaming-only and
tool-call-only APIs need an adapter. A nonempty key adds Bearer authentication;
leave it empty only for an unauthenticated server. Use HTTPS remotely; local
loopback HTTP is supported. Container loopback refers to the container, not the host.

LLM/MCP timeouts must be finite, greater than zero and at most 120 seconds; remaining
deadlines may shorten them. Restart API and worker after configuration changes.
`load_dotenv()` does not override process variables. Health reports configuration,
not provider connectivity: verify a worker-generated reply. Check worker liveness
and the shared DB for queued messages before diagnosing model failures. Do not
expose provider response details or keys in troubleshooting output.

TUI commands: `/new`, `/open ID`, `/run`, `/cancel`, and `/set target.repository=...`
(also `target.branch` / `target.profile`). F2 toggles settings. Chat replies answer
pending execution clarifications.

## Configuration ownership and fixed limits

This guide documents configuration; runtime code does not parse `AGENTS.md`.
Set deployment values through `.env` or process variables. `config.py` owns backend
settings; TUI reads `PERF_CHAT_API_URL` separately and accepts a `--url` override.
Web uses same-origin API/SSE paths, not a separately hardcoded backend address.
Do not introduce machine-specific IPs, absolute paths, tool names or credentials in
startup scripts. Keep `.env.example` and these instructions aligned with settings.

| Environment variable | Default / meaning |
| --- | --- |
| `PERF_CHAT_MODE` | `demo`; use `llm` for real model calls |
| `PERF_CHAT_EXECUTOR` | `demo`; alternatively `profile-demo` or `profile-mcp` |
| `PERF_CHAT_DB` | `data/perf-chat.sqlite3`; API and worker share the same file |
| `PERF_CHAT_API_HOST` / `PERF_CHAT_API_PORT` | `127.0.0.1` / `8000`; listener only |
| `PERF_CHAT_API_URL` | `http://127.0.0.1:8000`; TUI destination only |
| `LLM_BASE_URL` | `https://api.openai.com/v1`; replace for another provider |
| `LLM_MODEL` / `LLM_API_KEY` | Empty; model ID required in LLM mode, key required only by authenticated providers |
| `LLM_TIMEOUT_SECONDS` | `90`; finite, greater than zero, at most `120` |
| `PROFILER_MCP_URL` | Empty; `profile-mcp` requires this endpoint or `GIT_MCP_URL` |
| `PROFILER_MCP_TIMEOUT_SECONDS` | `60`; same timeout bounds as LLM |
| `PROFILER_MCP_ALLOWED_TOOLS` | JSON `[]`; explicitly permit server-specific read-only names |
| `PROFILER_MCP_PROFILE_ARGUMENTS` | JSON `{}`; optional legacy binding, unnecessary for new integrations |
| `GIT_MCP_URL` | Empty; optional provider-neutral Git Streamable HTTP MCP endpoint |
| `GIT_MCP_ALLOWED_TOOLS` | JSON `[]`; explicitly permitted read-only repository tools |
| `GIT_MCP_TIMEOUT_SECONDS` | `60`; finite, greater than zero, at most `120` |
| `GIT_MCP_TOKEN` | Empty; optional backend-only Bearer token, never sent to the model |
| `PERF_CHAT_GIT_REPOSITORIES` | JSON `{}` mapping aliases to worker-accessible paths |
| `PERF_CHAT_MODEL_INPUT_BYTES` | `120000`; accepted range `4096..1000000` |
| `PERF_CHAT_DOCS_MAX_INPUT_BYTES` | `2000000`; accepted range `4096..20000000` |
| `PERF_CHAT_SECRET_KEY` | No default; persistent Fernet key for non-Windows encrypted token storage |

Relative DB/repository paths and `scripts/export_contracts.py` depend on the working
directory. Run the exporter from the repository root: its fixed `schemas/` destination
is an intentional repository output path. Use an explicit working directory and
absolute deployment paths for services. Keep profiler documentation triggers in
`triggers.txt` and reference content in `Perf.md`, not in Python conditionals.

Some bounds are implemented in code, not exposed as environment settings. Do not
invent environment variables for them or describe them as operator configuration:

| Bound | Implementation / change point |
| --- | --- |
| 8 profiler calls, 16 planning rounds by default | `DynamicProfilerAnalyzer(max_calls=8)`; custom factory may set `1..30`, planning rounds are twice that value |
| 100 catalog tools per server, 20 pages, 1000000 serialized JSON characters per MCP payload | `integrations/mcp_gateway.py`; shared gateway validation limits |
| 5 source queries, 5 excerpts / 32 KiB combined | `dynamic.py`, `source_context.py`; per-range/blob/search bounds also enforced by source gateways |
| 16000 chat-report characters | `reporting.py` and `ChatReport` contract; keep both consistent |
| 100 durable steps per run | `worker.py`; independent of profiler-call and task iteration budgets |
| 600-second job lease | `store.py`; preserve retry/lease safety when changing it |
| Default run runtime 600 seconds, allowed `1..86400` | `PerformanceTask.budget.max_runtime_seconds` in `contracts.py`; a task field, not an environment setting |

Transport timeouts, queue polling, SSE heartbeats and client retry delays also have
local defaults. Change them at their owning boundary with appropriate validation;
do not confuse polling intervals or the job lease with the run execution deadline.
Synthetic fixture tool names/data are intentional test/demo constants, not a real
profiler contract. New deployment-specific values belong in configuration; new
security/size limits need explicit ownership, documentation and regression checks.

## Validation and documentation maintenance

Run checks appropriate to the change. Documentation-only edits need path/link and
consistency checks rather than a full runtime suite.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
node --test tests/web_state.test.cjs
```

If Windows temp permissions block pytest, use a fresh workspace directory with
`--basetemp=data/<unique-test-run>`. Preserve asynchronous state regression coverage.

| Changed area | Additional validation |
| --- | --- |
| Tokens/credentials | `tests/test_credentials.py` and Web tests; transport, non-echoing errors, active-run snapshots and cleanup |
| Consultation experts/intake routing | `tests/test_experts.py`, `tests/test_profiler_docs.py`, full Python suite and Web tests; task isolation, pending restart, replay ownership, context policy, budgets and failures |
| Dynamic MCP/analyzer lifecycle | `tests/test_dynamic_analysis.py`, full Python suite and Web tests; schema bounds, policy pinning, retries, questions and custom factories |
| Git MCP | `tests/test_git_mcp.py`; independent server routing, repository-only runs, ENV token isolation, schema/policy drift and deadlines |
| Context updates | `tests/test_context_updates.py`; confirmation/replay, replace/compare provenance, refresh drift, cumulative budgets, scoped sources/tokens and legacy custom analyzers |
| Source context | `tests/test_git_source.py`; immutable revisions, provenance, required selection and budgets |
| Report formatting | `tests/test_report_formatting.py`; validation, bounded Markdown and checkpoint reconstruction |
| UI layout | Browser inspection, collapsed sidebars and narrow screens |
| Contract models | Run `.\.venv\Scripts\python.exe scripts/export_contracts.py` and inspect generated diffs |

Update `README.md` for setup/product behavior changes and the relevant integration
guide for contract changes. Record actual critic findings, fixes and checks in
`REVIEW.md`; do not claim reviews or deployment validation that did not occur.
Keep README as the entry point, deployment.md for setup, integration-guide.md for
backend contracts and the specialist guides for extension details. Link to the
owning guide instead of duplicating startup commands or architecture diagrams.
Consolidate completed plan/review summaries in REVIEW.md rather than retaining
obsolete standalone plans. Empty Perf.md, trigger files, licenses, generated
contract schemas and the import-compatible GitLab adapter are intentional files.
See [deployment](docs/deployment.md), [integration](docs/integration-guide.md)
and [custom analyzers](docs/analyzer-extension.md) for detailed configuration.
See [Git MCP](docs/git-mcp.md) for provider-neutral repository access and its identity limitations.
