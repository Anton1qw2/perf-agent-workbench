# Backend consultation experts

The main chat supports task intake, consultations, mixed requests and capability
questions. The intake router runs before TaskAgent. Consultation turns do not update the task or its
version, do not require repository/profile settings and do not suggest Run.
Mixed requests update the task and consult one expert; task changes must be explicit
in the user's request. Routing is model-based in LLM mode and can misclassify intent.
Use `/ask <expert_id> <question>` to select consultation explicitly, or `/task <text>`
to select task intake. These are message prefixes, not new API endpoints or UI controls.

## Contextual routing and the shared work catalog

The routing catalog contains analysis/task preparation, assistant capabilities and
all registered consultation experts, including their input schemas. It describes
where requests go; it does not authorize tools. Analysis is not registered as a
consultation expert: TaskAgent prepares its specification and Worker executes it
through ExecutorGateway only after an explicit Run. Capability answers list the
analysis direction separately from the consultation experts and describe the selected
execution mode without asserting backend availability.

The router receives the current task summary, open questions and selected
profile/repository/branch, plus the latest run ID and status. Free-form task details,
run cursors, report contents, credentials and backend endpoints are excluded from
this context. The complete routing request still shares PERF_CHAT_MODEL_INPUT_BYTES.
This context is for routing; it does not grant experts task/history access.

With a selected profile or repository, questions about that selection's performance
or source default to task intake. "Why is CPU usage high here?" with a profile or
"Why is this function slow?" with a repository prepares analysis. Merely mentioning
a profiler does not select documentation. Explicit requests for documentation or
general facts stay consultations, and unrelated topics do not become analysis just
because a target exists. The current selection takes precedence over older history.
Mixed requests still require an explicit analysis/task request as well as consultation.
Missing input is clarified in chat; no route automatically starts an analysis.

Explicit commands take precedence. Short answers to an expert question continue
that consultation; a new analysis request exits it. Existing execution clarification
replies still use the resume endpoint and do not go through intake. LLM routing
classifies intent semantically; demo mode uses conservative English/Russian phrases
before the registered keyword matchers. It cannot interpret arbitrary paraphrases;
use `/task` or `/ask` when a demo shortcut is insufficient. Missing documentation
triggers still disable keyword-based expert selection.

## Assistant capabilities

Questions about the assistant itself, such as "What can you do?" or "Which agents
are available?", use the `capabilities` intent. The orchestrator returns a host-owned
description of supported functions, the worker's execution configuration and the
currently registered experts. It does not call TaskAgent, an expert factory or a
second model to generate this answer. Registration and configuration do not verify
documentation availability, backend connectivity or real performance measurements.
No endpoints, credentials or tool arguments are included in the description.
Capability descriptions use `ExpertRegistry.catalog(include_schemas=False)` to
read expert metadata without generating input schemas. The default `catalog()`
continues to include schemas for LLM routing.

Capability turns preserve the complete task and spec_version, need no repository
or profile, and do not append readiness questions or Run suggestions. They clear
pending expert questions and are excluded from subsequent TaskAgent history while
remaining visible in chat and routing history. Expired capability turns discard
their generated output and save only a host-owned timeout reply under the job lease.
LLM routing recognizes paraphrases in any language; demo mode uses a small set of
English and Russian phrases, including "What can you do?" and "Что ты умеешь?".
Explicit `/ask` and `/task` commands take precedence; requests to perform actual
analysis remain task intake.

## Register an expert

Registration happens in trusted Python startup code. There is no UI, dynamic import
from chat, plugin installer or arbitrary code upload. Supply an ExpertRegistry to
Worker; an explicit registry replaces the default registry. Start with
default_experts() to retain the profiler documentation expert.

```python
from perf_chat.config import Settings
from perf_chat.experts import ExpertDefinition, ExpertResponse
from perf_chat.experts.defaults import default_experts
from perf_chat.worker import Worker


class HardwareExpert:
    def ask(self, request):
        # Replace this limitation with your validated reference implementation.
        return ExpertResponse(
            status="unavailable",
            answer="No hardware reference service is configured.",
            limitations=["The user's machine configuration is unverified."],
        )


experts = default_experts()
experts.register(
    ExpertDefinition(
        id="hardware",
        name="Hardware expert",
        description="Explains CPU architecture and memory hierarchy.",
    ),
    factory=lambda services: HardwareExpert(),
)
worker = Worker(Settings.from_env(), experts=experts)
# Use the normal tick loop from your backend entry point.
```

Send `/ask hardware Explain cache associativity` to invoke this expert. The example
reports its missing backend; it does not synthesize hardware facts. The factory
receives ExpertServices(model=...) and may close over explicitly configured
read-only services. It is invoked once for the selected expert per intake turn.
Instances must not depend on surviving between jobs or Worker restarts.

ExpertDefinition.input_schema is a Pydantic model (default ExpertQuestion with
question). Use a model with extra="forbid" for custom inputs and declare optional
fields such as cpu_model/platform there. The router sees its JSON schema. Explicit
`/ask` supplies only question; additional required fields must therefore have a
default or be requested by the expert through needs_input. Invalid inputs produce
an unavailable response without calling the expert.
Validated input models are deep-copied directly into ExpertRequest. JSON serialization
is used for budgets, so Pydantic aliases, different validation/serialization aliases,
AliasPath and computed fields do not require a dump/revalidation roundtrip.

## Context, results and clarification

ExpertRequest carries validated inputs, an absolute monotonic deadline and the
context permitted by ExpertDefinition.context_policy. ContextPolicy defaults to
no task and no history. Set task=True and/or conversation_history=True only when
the expert requires those data; the host supplies independent copies. The router
can formulate a standalone question using chat history without passing that
history to the expert. Descriptions and model decisions never grant service access.

Return ExpertResponse with answer, status, sources, limitations and optional question.
Use status="needs_input" with a nonempty question to continue the consultation.
The host appends the question and renders source links/limitations. An expert should
return answer text without duplicating those rendered sections. Sources have title
and optional HTTP(S) URL; source accuracy and provenance validation belong to the
expert. Reference guidance and user answers are not measured evidence or agreement
to modify a task. Web/TUI retain their existing Markdown safety boundaries.

Worker stores the route, expert result and pending question atomically with the
assistant reply and job completion in the internal SQLite intake_turns table.
Pending expert selection survives restart. In LLM mode the router uses the pending
input/question to resolve a follow-up; in demo mode it combines the original
question, expert question and user reply into question text. Demo mode does not
infer custom structured fields or mixed intent. `/task` and `/ask` override pending
selection. A removed expert produces a limitation, without modifying the task.
Transient routing/expert failures retain the pending question so a short answer
can be retried after restart. A terminal answer/not_found clears it; explicit /ask
starts a new consultation, and task intake exits the pending consultation.

Consultation user/reply indices are excluded from subsequent TaskAgent inputs but
remain in chat history for routing. For mixed turns, subsequent extraction sees
only the task portion of the assistant reply rather than expert guidance.
Old databases need no destructive migration; the new table is created automatically,
and previously unclassified messages retain
their existing interpretation. Pure consultations preserve spec_version. Existing
intake.completed events still carry the current task version and trigger snapshots.
Existing execution clarification handling keeps priority at the API boundary.

## Budgets and trust

The initial limits are host-owned: 32 registered experts, one selected expert call
per turn, and a 120-second Worker intake deadline shared by routing, task extraction,
expert calls and task response generation. The serialized routing request and expert
input/context/result each use PERF_CHAT_MODEL_INPUT_BYTES. The common response caps
answer at 16000 characters, sources at 64 and limitations at 16 (2000 characters
each). Registry errors and late results return a generic unavailable response;
provider exception text and invalid payloads are not echoed. Routing failure leaves
the task unchanged. The complete turn may fail if a task/model call exhausts its
deadline. If an expired graph returns a pure consultation, Worker discards the entire
generated result and saves only its own unavailable reply and classification/indices.
It retains the previous question only when the turn continued that consultation;
an explicit /ask or a different selected expert clears it even on timeout. The
normal and expiry paths share the same selection rule. The user message remains excluded from future task
extraction. Expired task/mixed results are rejected without task changes.
Synchronous custom Python cannot be forcibly interrupted: implement
bounded transport timeouts, honor request.deadline and use the runtime deadline.

Factories and experts are trusted application code, not sandboxed plugins. They
must use read-only operations: jobs can be redelivered before commit. SQLite lease
ownership suppresses duplicate commits; it cannot prevent repeated external calls.
Do not inject Store, execution gateways or tokens into model-visible context.
Credentials belong only in backend service transports, bound to destinations.
Existing MCP allowlists and schema/policy checks still apply to any injected MCP
service. Expert descriptions, reference documents and user input cannot authorize
additional tools or writes.

## Profiler documentation expert

ProfilerDocsAgent directly implements the common ask(ExpertRequest) interface.
It is registered in defaults.py with ExpertDefinition, DocumentationQuestion and
the default ContextPolicy, just like an application expert. There is no adapter,
documentation-specific graph node, output field or orchestration branch. Its
registry response uses the same answered/not_found/unavailable statuses and source
rendering as other experts. Detailed document-load limitations remain in answer text.

The original ask(question, version, platform) and as_tool() APIs remain available
for the analysis workflow and external callers; they return DocumentationAnswer.
Both entry points use the same document loader. The document expert's own policy
uses only question/version/platform and the full document, with no task, history,
measurements or source code. Its separate PERF_CHAT_DOCS_MAX_INPUT_BYTES limit is
unchanged.

All registered experts appear in the LLM routing catalog regardless of demo matchers.
Profiler documentation is selected by its description and schema, just like hardware
or custom experts. No keyword is required in LLM mode. Optional matcher(question)
functions support deterministic demo selection only. The default documentation
matcher rereads triggers.txt on each turn; missing/unreadable/empty triggers disable
that demo shortcut. Explicit `/ask profiler_docs ...` works in either mode.
