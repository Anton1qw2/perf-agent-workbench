# Built-in and custom profile analyzers

## Default behavior

`PERF_CHAT_EXECUTOR=profile-mcp` now uses `DynamicProfilerAnalyzer`. It discovers the
server's paginated MCP tool catalog rather than requiring three specific methods.
`PERF_CHAT_MODE=llm` enables dynamic selection and interpretation. Without a model it
returns the catalog and an explicit limitation; it does not guess tools or arguments.
`profile-demo` retains the original deterministic graph and synthetic profiler data.
The existing `ProfileAnalysisAgent` class remains available for compatible legacy graphs.

The built-in LangGraph executes one node per durable worker step:

```text
discover -> plan -> call -> plan -> ... -> report
              |                       |
              +-> clarification       +-> source/context and interpretation
```

The planner follows the packaged `instructions/dynamic_analysis.md`: establish context,
find relevant measured costs, obtain useful detail and finish when evidence is sufficient.
It selects only real tools and returns a validated JSON decision with a reason. There is
no requirement that a profiler expose a metric catalog or use particular method names.
The current profile reference remains an opaque `result_id` in the application contract.
A profile executor still asks for this reference if the task starts with only Git.
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

Install `.[mcp]`, configure the model and profiler endpoint, and explicitly authorize
trusted read-only tool names on both API and worker. For a server exposing
`inspect_session(session=...)`, for example:

```dotenv
PERF_CHAT_MODE=llm
PERF_CHAT_EXECUTOR=profile-mcp
PROFILER_MCP_URL=https://profiler.example.com/mcp
PROFILER_MCP_ALLOWED_TOOLS=["inspect_session"]
PROFILER_MCP_PROFILE_ARGUMENTS={"inspect_session":"session"}
```

Use your actual server's names and argument keys. The default allowlist contains
`get_list_metrics`, `get_hotspots` and `get_asm` for compatibility. Every discovered
tool is recorded, but the planner receives only allowed tools. Server descriptions
and read-only annotations do not grant permission. The allowlist is an operator trust
decision, not proof that a server has no side effects. This iteration has no automatic
approval or execution of unknown tools and no arbitrary module loading from chat.

The profile-argument map binds a tool's top-level identifier to the current Result ID.
Unmapped tools have no enforced profile-argument binding; explicitly review their
scope and argument semantics before allowing them. Never use mappings to guess the
meaning of undocumented metrics or identifiers. Tokens stay in backend transport
headers and are not passed to the planner or checkpoint arguments.

Input arguments and declared output schemas are validated with JSON Schema. Schema
references cannot fetch remote documents. Definitions are checked again immediately
before a call; changes require a new run. No output schema is required: unknown JSON
or text is retained as evidence without invented fields, units or metric calculations.
Image/audio-only tool results are not supported by the current text model adapter.

Limits: 100 tools, 20 discovery pages, 1 MB decoded catalog/response, eight profiler
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
- `source`: bounded Git/GitLab gateway using immutable credential references.
- `max_input_bytes`: model request budget.

The synthetic demo gateway retains the legacy profiler methods; it does not pretend
to be an arbitrary MCP server. A custom graph can inspect `services.profiler.demo`.
A replacement MCP gateway used by the built-in analyzer must implement `MCPToolGateway`,
including a stable `configuration_fingerprint` for its endpoint and execution policy.
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
