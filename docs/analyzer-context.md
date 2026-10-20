# Updating analyzer context during a run

An analyzer may request a missing/new profile, repository or branch without changing
the original task snapshot. Updates are versioned, explicitly confirmed and persisted
before any new source/model/MCP work. The feature replaces the former Git-only to
profiler limitation. Active v3 built-in runs must restart with the v4 analyzer;
existing reports remain readable through default optional fields.

## User flow

1. The analyzer asks a normal chat question, for example which Result ID to inspect.
2. After receiving the answer, it proposes a typed context update. The analyzer owns
   answer interpretation; Worker and Store do not parse profiler tool names or IDs.
3. The host displays the old/new context and `replace`/`compare` mode in chat.
4. Reply `yes` or `y` to apply (the Russian affirmative is also accepted). Any other reply keeps the old context and resumes
   analysis with rejection feedback. Confirmation answers are separate from analyzer Q/A.
5. Acceptance commits an ordered ledger entry as a progress step. On the next worker
   steps, source preparation and catalog refresh use the accepted context.

Changing Project settings alone still does not modify an active run. Updates occur
only through this analysis clarification/proposal/confirmation path, while the run is
active. A completed run requires a new run. Tokens stay in credential settings and
are never accepted as context patch fields.

## Contracts and ownership

`AnalysisContext` contains revision (`0..8`), `result_id`, `repository` and `branch`.
Revision zero is the initial analysis context. `run.task` remains unchanged; the
host cursor stores `analysis_context_initial` and ordered `context_updates`.

`ContextUpdateProposal` contains:

```json
{
  "expected_revision": 0,
  "values": {"result_id": "new-profile-id"},
  "mode": "replace"
}
```

Only the three context fields are allowed. Omitted fields remain unchanged. Null
clears repository/branch; an empty string clears Result ID. Empty repository/branch
strings and null Result ID are rejected. At least a profile or repository must remain.
Changing repository without specifying branch clears branch. Values are validated
again after merging, including lengths and credential-free repository URLs.
Clearing Result ID requires an enabled Git MCP gateway and repository; it is rejected
when the run would fall back into the initial Result ID bootstrap flow.

The host accepts proposals only from opt-in analyzers after an answered analyzer
question. It validates `expected_revision` both before showing the confirmation and
when accepting it. Confirmation IDs are correlated with replies. Each accepted
`AnalysisContextUpdate` includes deterministic `update_id`, original `reply_to`,
previous/current contexts and mode. Replaying the same leased confirmation step
produces the same entry, not another revision. At most eight updates are accepted.
Stale/invalid proposals and declined confirmations retain the previous context.

The analyzer owns its JSON checkpoint and planning algorithm. Worker owns confirmed
context, persistence, credentials and source preparation. API/Store/UI keep their
existing general event/clarification flow; no profiler-specific UI form is required.

## Built-in evidence rules

| Update | Active model evidence |
| --- | --- |
| Add a profile to Git-only context | Keep Git evidence for the same repository/branch; discover profiler tools |
| Replace a profile | Exclude prior profile responses; keep matching Git evidence |
| Compare profiles | Keep both profile scopes only for the same repository/branch |
| Change repository or branch | Exclude old scoped profiler/Git evidence; prepare a new source snapshot |

Every new MCP evidence record stores its immutable `context`, including Result ID,
repository, branch and revision. Full historical responses remain in the final
report; only active evidence is passed to planning/interpretation. Citation checks
accept only IDs actually delivered to the current model. Reports display context
updates and evidence scope. Comparison quotes server-provided values; it does not
enable derived metric calculations or prove source/profile-build linkage.

Catalog refresh is nondestructive: evidence, questions, counters and deadlines remain.
Actual discovery results, including an empty list, must match previously pinned tool
definitions. A previously inactive profiler can be discovered after profile addition.
Changing endpoint, allowlist or selected schemas still fails closed.

Explicit repository/ref changes archive previous source snapshots and cached excerpts.
Old scoped commits, locations and symbols are cleared; `source.policy` is preserved.
Required source selection is clarified again. Unchanged source scopes reuse their
accepted snapshot even if the live branch has moved. User confirmation authorizes
the new scope; it does not establish a build/profile mapping.

Personal Git tokens remain bound to their original repository and MCP endpoint;
they are not forwarded to a newly selected repository. A configured ENV service
token may be used as the existing fallback. No context update changes authentication,
permissions, endpoints, goal or budgets. MCP calls/rounds, source limits and run runtime
are cumulative across all context revisions.

## Custom analyzers

Legacy implementations keep the previous `step_analysis` signature. To opt in:

```python
from perf_chat.profiler_analysis_agent import AnalysisStep, ContextUpdateProposal


class MyAnalyzer:
    analyzer_id = "my-analyzer-context-v1"
    supports_context_updates = True

    def step_analysis(self, request, *, checkpoint, answer=None, deadline=None,
                      source_snapshot=None, context_updates=None):
        # Restore your graph. Consume the accepted context_updates and current
        # request.context_revision; handle request.context_feedback after rejection.
        # The example assumes your graph already asked a question and got its answer.
        if answer is not None:
            return AnalysisStep(
                checkpoint=checkpoint,
                context_update=ContextUpdateProposal(
                    expected_revision=request.context_revision,
                    values={"result_id": answer},
                ),
            )
        # Otherwise advance your own graph and return progress, a question or report.
        ...
```

New kwargs are passed only when `supports_context_updates=True`. The accepted ledger
is supplied as typed `AnalysisContextUpdate` objects. The host does not prescribe
your graph nodes or context registry. Your graph must handle revised input, keep
provenance and citations correct, and persist retry-safe state. It cannot bypass
confirmation by mutating a request or returning a proposal inside its checkpoint.

The synchronous built-in `analyze()` convenience method cannot confirm a proposal;
it returns a needs-input report directing the caller to the durable `step_analysis`
lifecycle. The worker integration supplies the confirmation/ledger machinery.
