# Built-in and custom profile analyzers

## Default behavior

`PERF_CHAT_EXECUTOR=profile-mcp` now uses `DynamicProfilerAnalyzer`. It discovers the
server's paginated MCP tool catalog rather than requiring three specific methods.
`PERF_CHAT_MODE=llm` enables dynamic selection and interpretation. Without a model it
returns the catalog and an explicit limitation; it does not guess tools or arguments.
`profile-demo` uses the same dynamic graph with a synthetic MCP catalog/data and a
deterministic model fixture when LLM mode is off. The fixed analyzer and its old
three-method gateway protocol have been removed. Import `DynamicProfilerAnalyzer`
or use `create_builtin_analyzer(services)`; old analyzer checkpoints require a new run.

Demo classification is host-owned: Worker derives `AnalysisServices.demo` only from
`PERF_CHAT_EXECUTOR=profile-demo`. The built-in analyzer uses that flag for fixture
selection, model context and report labeling; it never reads gateway demo attributes
or MCP response fields to select execution mode. `profile-mcp` always supplies False,
independently of `PERF_CHAT_MODE`. Checkpoints pin this flag; changing it requires a
new run. Saved reports retain their original labels. This classification does not
verify whether a remote service supplied genuine measurements.

The built-in LangGraph executes one node per durable worker step:

```text
discover -> plan -> call/source -> plan -> ... -> report -> format_report
              |
              +-> clarification -> resume
              +-> context proposal -> confirmation -> commit -> refresh -> plan
```

The planner follows the packaged `instructions/dynamic_analysis.md`: establish context,
find relevant measured costs, obtain useful detail and finish when evidence is sufficient.
It selects only real tools and returns a validated JSON decision with a reason. There is
no requirement that a profiler expose a metric catalog or use particular method names.
A second optional [Git MCP gateway](git-mcp.md) supplies provider-neutral repository
tools. Decisions include `server=profiler|git`; the eight-call budget is shared.
The current profile reference remains an opaque `result_id` in the application contract,
with an empty default for Git-only analysis.
The request also carries optional `repository` and `branch` fields. The worker passes
user-selected repository metadata even when source access is unavailable. Explicit local
Git paths are scoped to the run without clone/fetch; symbolic aliases require backend
registration. Optional local source defaults to a pinned `HEAD` when no branch was set.
The planner sees the source snapshot outcome and may choose `action=source`, specifying
bounded `locations` or `symbols`, only when it helps the investigation. Source reads use
that immutable revision, with at most five queries and five combined excerpts/32 KiB.
Collected code is delivered to later planning and interpretation under the common budget.
It never proves correspondence with the profile build. Required-source user constraints
and explicitly disabled source remain effective; required user-selected ranges are
collected independently of planner selections. Planner-selected excerpts carry
`origin=model_selection` (and `resolution=model_location` for explicit ranges).
They cannot replace required user-selected ranges. The planner cannot directly
change repository, revision, authentication or permissions.
It may propose a context update; the host validates and persists it only after
explicit correlated confirmation. See [context updates](analyzer-context.md).
A repository-only task uses Git MCP without a profile reference when that gateway
is configured; otherwise the profile executor asks for a Result ID.
The current transport opens a new MCP session for discovery/calls. Tools must accept
durable identifiers and independent read-only requests; session-local handles or
stateful tool protocols need a custom gateway/analyzer.

The cursor retains `analyzer_id`, `analyzer_state`, pinned tool definitions, decisions,
responses, clarification answers and counters. The checkpoint also pins a SHA-256
`connection_fingerprint` of the MCP endpoint, allowlist and profile-argument mapping.
Raw endpoint parameters and token values are not included in that fingerprint field. A planned call is checkpointed before
execution; a successful response is persisted before planning again. An uncommitted
read-only call can be redelivered after a crash. Do not enable mutating tools.
Changes to an analyzer's identity, endpoint, allowlist, profile-argument mapping or a
selected tool definition fail closed and require a new run. Do not resume an existing
analysis against a different profiler server even if it exposes identical schemas. The analyzer factory may be recreated on each step and must not rely on
in-memory graph state surviving. Catalog metadata is not a source of measurements.

## Configure a profiler

When upgrading from the fixed default names, explicitly set your allowed tool list;
an unset list now allows no calls. Existing explicit legacy bindings remain effective.
Restart API and worker after configuration changes and start a new analysis when the
pinned policy changes; existing runs must not silently adopt another policy.

Install `.[mcp]`, configure the model and profiler endpoint, and explicitly authorize
trusted read-only tool names on both API and worker. For a server exposing
`inspect_session(session=...)`, for example:

```dotenv
PERF_CHAT_MODE=llm
PERF_CHAT_EXECUTOR=profile-mcp
PROFILER_MCP_URL=https://profiler.example.com/mcp
PROFILER_MCP_ALLOWED_TOOLS=["inspect_session"]
```

Use your actual server's tool names; do not configure argument names or values.
The default allowlist is empty. Every discovered tool is recorded, but the planner
receives only allowed tools. Server descriptions
and read-only annotations do not grant permission. The allowlist is an operator trust
decision, not proof that a server has no side effects. This iteration has no automatic
approval or execution of unknown tools and no arbitrary module loading from chat.

The planner builds arguments from each discovered input schema and tool description,
the current task/profile reference, prior evidence and user clarifications. Nested objects,
arrays, enums and tools with no arguments need no manual templates. Unknown mandatory
values produce focused chat questions rather than invented values. Invalid arguments
are rejected before a profiler call and returned to bounded planning as schema feedback;
the gateway revalidates them before execution. No dynamic behavior is keyed to a
particular tool name. `include_asm` is a planner preference by capability, not a backend
permission check based on names. The allowlist remains the execution authorization.

`PROFILER_MCP_PROFILE_ARGUMENTS` is still accepted as an optional legacy constraint.
It defaults to `{}` and is not needed for new integrations. If explicitly set, it binds
those top-level identifiers to the current Result ID and remains pinned with policy.
Without it, JSON Schema validates structure, not profile identity; the planner must
respect known identifiers and reports retain this limitation. Neither argument names
nor undocumented metric/identifier semantics are guessed by host code. Tokens stay
in backend transport headers, never planner context or checkpoint arguments.

Input arguments and declared output schemas are validated with JSON Schema. Schema
references cannot fetch remote documents. Definitions are checked again immediately
before a call; changes require a new run. No output schema is required: unknown JSON
or text is retained as evidence without invented fields, units or metric calculations.
Image/audio-only tool results are not supported by the current text model adapter.

Limits: 100 tools, 20 discovery pages, 1000000 characters of serialized JSON per
catalog/response, eight profiler
calls and sixteen planning rounds per analysis. Planning and interpretation use the
shared model input budget. Calls share the run deadline and transport/model timeouts;
late success is rejected. When the investigation budget is reached, existing evidence
can still be interpreted with an explicit limitation, provided at least one response
is structurally nonempty. `null`, empty arrays/objects and whitespace-only text are
retained as raw evidence but cannot alone trigger a completed interpretation, either
at budget exhaustion or when the planner chooses to finish. Numeric zero and boolean
false are not automatically discarded; their semantics still depend on the server.
Invalid plans, unknown citations and insufficient context produce a bounded failure
report retaining previously accepted evidence.

## Replace the analyzer in Python

The public boundary is `ProfileAnalyzer.step_analysis`, returning `AnalysisStep`.
The default is selected through `create_builtin_analyzer(AnalysisServices)`.
Pass a different factory to `Worker`; Web, TUI, API and Store need no changes:

```python
import time

from perf_chat.config import Settings
from perf_chat.worker import Worker
from my_application.analysis import MyAnalysisGraph


def create_my_analyzer(services):
    return MyAnalysisGraph(services)


worker = Worker(Settings.from_env(), analyzer_factory=create_my_analyzer)
while True:
    if not worker.tick():
        time.sleep(0.3)
```

Keep `PERF_CHAT_EXECUTOR=profile-mcp` (or `profile-demo` for a synthetic integration)
when using this path. `PERF_CHAT_EXECUTOR=demo` selects the separate demo executor.
Supplying an explicit `executor=` still takes precedence over `analyzer_factory=`.
The factory is trusted backend code and receives services for the immutable run:

- `analysis_id`: stable run ID, suitable for a custom LangGraph thread mapping.
- `profiler`: configured gateway with catalog discovery and validated calls in MCP mode.
- `model`: shared model adapter, or `None` in demo mode.
- `source`: bounded local Git gateway using immutable source snapshots.
- `git`: optional provider-neutral Git MCP gateway using backend ENV authentication.
- `max_input_bytes`: model request budget.
- `demo`: host-configured execution classification, True only for `profile-demo`.

The synthetic demo gateway implements the same discovery/call protocol with a fixed
fixture catalog and synthetic responses; it has no legacy named profiler methods.
A custom graph should use `services.demo`, not gateway metadata. When constructing
`DynamicProfilerAnalyzer` directly, pass `demo=True` explicitly for synthetic fixtures;
the default is False. The gateway protocol does not require a demo attribute.
A replacement MCP gateway used by the built-in analyzer must implement `MCPToolGateway`,
including a stable `configuration_fingerprint` for its endpoint and execution policy.
Git and profiler fingerprints/catalogs are checkpointed separately. The current built-in
identity is `builtin-dynamic-mcp-v4`; v3 active runs require restart. Git evidence carries
`server="git"`, and the report retains `git_tool_catalog` separately.
Use the MCP gateway's `call_tool` method for schema/policy enforcement and
`PromptContextBuilder` for the common model input/citation budget. Custom graphs
are responsible for enforcing their own algorithm, iteration limits and citations.

The minimal lifecycle implemented by your class is:

```python
def step_analysis(self, request, *, checkpoint, answer=None,
                  deadline=None, source_snapshot=None):
    # Advance your own graph by one bounded, retry-safe step.
    # Restore state from checkpoint or a persistent checkpointer keyed by analysis_id.
    # Return AnalysisStep(checkpoint=state, message="...") for progress,
    # or AnalysisStep(checkpoint=state, report=report) for a report/question.
    ...
```

Define a stable `analyzer_id`, including a version, on your implementation. Its report
is a `ProfileAnalysis`: `completed`, `needs_input`, or `insufficient_data`, with evidence,
observations, hypotheses and limitations. Do not equate `completed` with successful
optimization. A question uses `clarification_field="analysis_input"` and `open_questions`;
the next call receives the correlated plain-text answer. The built-in analyzer passes
ordered question/answer pairs to both the planner and final interpretation through the
common model input budget, so answers such as "the second one" keep their context
after a worker restart. Clarifications describe user scope and assumptions, not
measured evidence. Custom graphs must preserve equivalent clarification context.
`metrics` and `source` retain
the existing structured clarification paths. Checkpoints must be JSON-serializable
and free of credentials. `cancel(run_id)` is optional for releasing custom resources;
worker cancellation remains cooperative between steps.

Opt-in context updates preserve this boundary: set `supports_context_updates=True`
and accept the optional `context_updates` keyword to propose typed changes after
answered questions. Host confirmation and persistence are generic. See
[context update contracts](analyzer-context.md) for the complete lifecycle and
backward compatibility; legacy analyzer signatures receive no new kwargs.

Source preparation is persisted by the executor before analysis. Use the supplied
`source_snapshot`; never re-resolve an accepted source revision. A custom graph may
choose additional source nodes, but must preserve the existing scope, limits and
unverified relationship between source commits and profiled builds.

For synchronous callers, `DynamicProfilerAnalyzer(...).analyze(request)` runs these
steps until a report/question is available. Use `step_analysis` and persist checkpoints
when resuming questions or recovering after a process restart. The built-in algorithm
is a starting point; the same boundary accepts a more explicit custom LangGraph.

## Validation status

Tests cover nonstandard tool names, paginated discovery, JSON schemas without network
resolution, allowlists, profile bindings, schema drift, context budgets, raw evidence,
citation rejection, plain-text clarification, worker reconstruction and custom factories.
A real local Streamable HTTP MCP fixture was exercised with a deterministic model stub.
Private profiler deployments and real-model planning still require integration validation.
Three review/fix iterations and the final checks are recorded in [REVIEW.md](../REVIEW.md).


## Chat report boundary

The durable `report` node validates interpretation and retains the original response
in `analyzer_state.raw_interpretation`. Complete JSON code fences are accepted before
schema and evidence-ID validation. Other free-form output or invalid citations cannot
become validated findings: the report remains `insufficient_data` and stores no accepted
observations from that response.

A separate `format_report` node then creates `ProfileAnalysis.chat_report`, an optional
backward-compatible `{format: "markdown", content: "..."}` field. Formatting is
deterministic, makes no model calls and caps chat content at 16000 characters. It uses
headings/lists, escaped text, fenced unvalidated excerpts and registered documentation
links; unsupported HTML/images are reduced to text. Raw profiler responses and code are
kept in the run endpoint, not dumped into the chat report. Display shortening is explicit.
Custom structured reports pass through the same formatter in the executor, including
reports that already supplied their own chat presentation. The formatter never repairs
missing evidence by inventing facts or turns invalid responses into successful analysis.
Reconstructing the worker at the formatting checkpoint must not repeat model/tool calls.
