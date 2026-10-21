# Code review notes

## Contextual intake routing and shared work catalog

Date: 2026-10-21. Implementation self-review; no independent critic or live model/MCP
deployment validation was performed. Routing fixtures verify the request contract
and isolation behavior, not arbitrary intent recognition by a deployed LLM.

The router previously saw expert descriptions and history but no authoritative
current target, making profile-analysis requests vulnerable to documentation routing.
It now receives a shared catalog of task analysis, capabilities and consultation
experts, plus whitelisted current summary/open questions/profile/repository/branch
and latest run ID/status. Details, run cursors/results and transport configuration
are not added to that context. Input-size enforcement and expert ContextPolicy remain.

Selected-target performance/source questions default to analysis intake. Explicit
documentation, general facts, unrelated topics, capabilities and commands retain
their separate paths. Demo shortcuts prioritize explicit analysis and a bounded set
of contextual questions over documentation keyword matches. New analysis exits pending
expert questions; short replies preserve them. Analysis is not a consultation expert
and no intake route launches a run. TaskAgent explicitly supports analysis tasks and
retains the opaque MCP result_id rule. Capability answers now list analysis separately.

Self-review tightened demo shortcuts so requests to find documentation do not become
mixed task changes and source-specific questions are not classified as general facts
solely because they start with "What does". Added an explicit instruction that only
consultation catalog entries may populate expert_id.

Validation: 288 Python tests, 26 Web state tests, Ruff, diff whitespace and local
documentation-link checks passed. Coverage includes profile/repository readiness
through the API with no automatic run or documentation invocation, explicit docs
preserving task/version and expert isolation, missing-source clarification, stale
history versus current selection, run-context whitelisting, router budgets, built-in
direction rejection as an expert, and switching from pending docs after Worker restart.
Updated AGENTS.md, README.md and the consultation extension guide. No public contract
models or generated schemas changed. Execution clarification routing remains unchanged.

## Host-owned analyzer execution mode

Date: 2026-10-21. Implementation self-review; no independent critic or live MCP/LLM
deployment validation was performed.

The built-in analyzer previously read the gateway's demo attribute for fixture
selection, interpretation context and report labels. Worker now derives
`AnalysisServices.demo` from `PERF_CHAT_EXECUTOR=profile-demo` and passes it explicitly
through the factory. MCP metadata and response fields cannot change that classification.
The gateway protocol no longer requires demo metadata; direct analyzers default to
False and synthetic fixtures pass True explicitly. Configuration is not proof that
remote data are genuine measurements.

Standalone analysis uses the same executor configuration. The legacy `--demo` flag
cannot override it, and standalone use requires profile-demo or profile-mcp. Checkpoints
pin the host mode and reject changes before further calls. For older checkpoints
awaiting formatting, the saved report flag detects conflicting mode changes.

Validation: 263 Python tests and 26 Web tests passed; Ruff, diff whitespace and local
documentation-link checks passed. Contract schemas were regenerated without changes.
New regressions contradict host mode with both gateway metadata and response data,
reconstruct Worker between steps, check model payloads/report labels/result status,
verify fixture selection, reject current/legacy checkpoint drift, and exercise
standalone dotenv configuration and conflicting command-line flags.

### Focused execution-mode refactoring

Date: 2026-10-21. A delegated implementation agent performed a focused refactor
and self-review; this was not an independent critic review or deployment validation.

Removed the standalone CLI's duplicate MCP-endpoint check: `Settings.from_env()`
already rejects profile-mcp without a profiler or Git endpoint. Worker now derives
its demo boolean once for both gateway selection and AnalysisServices. Git gateway
construction and token lookup share one explicit profile-mcp branch, and synthetic
profiling no longer performs an unused profiler credential lookup. No additional
abstraction or prompt changes were introduced. The host-owned mode, CLI restrictions,
checkpoint pinning and saved-report compatibility remain in place.

Validation: all 263 Python tests, 26 Web state tests, Ruff and diff whitespace checks
passed after the refactor. No commit or push was performed.

## Assistant capability routing

Date: 2026-10-21. Implementation self-review; no independent critic or live provider
validation was performed.

The router previously offered only task, consultation and mixed intents, so questions
about the assistant could fall into an unavailable-expert response. Added capabilities
routing and a deterministic orchestrator response using supported functions, worker
execution configuration and the actual expert registry. Expert metadata is bounded and
Markdown-escaped; factories and answer-generation models are not invoked.

Self-review identified two related persistence requirements: capability turns must
neither increment spec_version nor enter later task extraction, and late capability
responses must be discarded while retaining their classification. Store and Worker
now handle both under existing lease and deadline checks. Capabilities exit pending
expert questions. Explicit commands and existing unknown-expert validation retain
their behavior; configuration and registration do not establish backend connectivity.

Validation: full Python suite (251 passed), Web state suite (26 passed), Ruff and
Git diff whitespace checks passed. Contract schemas were regenerated with no changes;
IntakeRoute is internal and is not part of the exported API/executor schemas.
Regression coverage includes demo and model routing, task/history isolation across
restart, pending-question switching, lease/replay ownership, expired output rejection,
configuration reporting without endpoints/tokens and bounded expert descriptions.

### Intake refactoring and redundant-code check

Date: 2026-10-21. Self-review of capabilities, intake routing, expert registration
and the Worker/Store integration; no independent critic was used.

- Removed the intake graph's unused missing_fields output and validate node.
  OrchestratorAgent still computes readiness for its response; Store still validates
  persisted tasks and launch requirements at the existing boundary.
- Capability rendering now requests registry metadata without generating expert input
  schemas. The default schema-bearing catalog remains compatible with LLM routing.
- Expired capability turns no longer save a fabricated consultation result. Their
  route, timeout reply, message isolation and cleared pending question remain durable.
- Combined duplicate route normalization and replaced demo punctuation regex handling
  with string operations. Unknown-expert checks and explicit command precedence remain.

Validation: 252 Python tests and 26 Web tests passed; Ruff and diff whitespace checks
passed. Added a regression ensuring capability descriptions do not generate schemas,
and asserted that expired capability records contain no consultation result.
This was a focused intake review, not a claim that all repository code is unused-code-free.

## Dynamic MCP analyzer: three iterations

Date: 2026-10-18. Scope: the built-in dynamic profiler analyzer, MCP gateway,
replaceable analyzer lifecycle, durable checkpoints and supporting documentation.
An independent critic reviewed the implementation in three review/fix iterations.

### Iteration 1

Three findings were confirmed and fixed:

1. **P1: empty evidence at budget exhaustion.** The budget path tested for the presence
   of evidence records, while the finish path tested response contents. With one allowed
   call returning `[]`, interpretation could run and report completion. Both paths now
   use `_has_usable_evidence`. Null, empty containers and whitespace-only text remain
   in raw evidence but do not alone permit interpretation. Zero/false are preserved.
2. **P2: clarification context missing from final interpretation.** Planner answers
   were retained in the checkpoint but omitted from the final model request. The
   interpretation context now includes user clarifications under the common input budget.
3. **P2: endpoint and policy drift across durable steps.** A reconstructed worker could
   use another MCP endpoint with identical tool schemas. A gateway configuration
   fingerprint now pins the endpoint, allowlist and profile-argument bindings. A mismatch
   fails before further calls; raw endpoint parameters and tokens are not stored in the
   fingerprint field. Tool definition drift remains separately checked before execution.

Regression tests cover both finish paths, pending-call configuration changes,
secret-free fingerprint storage and clarification delivery to interpretation.

### Iteration 2

The critic confirmed all three fixes and independently ran 34 dynamic tests.
One additional **P2** finding was fixed: the stateless planner received an answer
without its previous question. A relative answer such as "the second one" therefore
lost meaning before the next tool decision. One `_clarification_context` now produces
ordered question/answer pairs for both planning and interpretation. Tests reconstruct
the analyzer between steps and assert the relative answer retains its question.

### Iteration 3

The critic found no remaining actionable findings and independently ran all 35 dynamic
tests. The final review covered the four fixes, the gateway protocol, durable restart,
custom analyzer factories, source snapshot preservation and policy/schema boundaries.
Documentation and AGENTS.md were then updated to reflect the reviewed behavior.

### Final validation

- Full Python suite: **151 passed**.
- Web state regression suite: **16 passed**.
- Ruff checks for source, tests and scripts: passed.
- Model-input budgets, no-network JSON Schema resolution, schema drift, evidence
  citation checks and active-run analyzer identity remain covered by regression tests.
- The local Streamable HTTP MCP fixture was previously exercised with a deterministic
  model stub; it is not evidence of real-model or private-server compatibility.

Remaining integration scope: real LLM providers and private MCP profilers need validation
against their deployed tool schemas, semantics, authentication and session behavior.
The current transport expects independent read-only requests with durable identifiers;
session-local state needs a custom gateway/analyzer. Custom analyzers are trusted backend
code and remain responsible for their algorithm, checkpoints, budgets and citations.


## Configurable documentation triggers: three iterations

Date: 2026-10-19. Scope: commit `8b1b5a4`, editable documentation routing,
package data, replacement knowledge content and public instructions.

1. **Iteration 1:** no confirmed defects. The critic checked intake routing,
   literal keyword matching, reload behavior, package configuration and tests.
2. **Iteration 2:** no confirmed defects. Additional checks covered UTF-8 BOM,
   invalid UTF-8, missing/unreadable files, Unicode word boundaries, literal
   punctuation and phrases, document replacement and per-message context.
3. **Iteration 3:** one **P3** documentation issue was found and fixed.
   `docs/integration-guide.md` still described a hardcoded Perf trigger. It now
   documents `triggers.txt`, live reload and disabled routing for unavailable or
   empty configuration, consistent with README and AGENTS.md.

Validation: **152 Python tests passed**, **16 Web state tests passed**, Ruff passed.
The wheel was built and inspected: it includes `triggers.txt`, its loader and
`Perf.md`. Triggers control automatic chat documentation routing only; they do not
invoke MCP tools or change the analyzer's report-time lookup. Keywords and phrases
are literal; phrases preserve their internal whitespace. Knowledge remains a
single replaceable UTF-8 Markdown document.


## Compact execution status and clipboard: two iterations

Date: 2026-10-19. Scope: the Web execution status panel and full-text copy button.

1. **Iteration 1:** no confirmed defects. The critic reviewed fixed two-line height,
   internal scrolling, hidden panel behavior, Clipboard API and HTTP fallback,
   temporary element cleanup, focus restoration and asynchronous feedback guards.
   Two additional regression scenarios were recommended and added: a status update
   and a conversation switch while a clipboard operation is pending.
2. **Iteration 2:** no confirmed defects. The critic verified the new pending-copy
   tests, independently ran all **21 Web state tests**, and checked the final diff.
   Copy retains the clicked text snapshot and does not mark new status text as copied.

Validation: **21 Web state tests passed**, JavaScript syntax and `git diff --check`
passed. A real headless Edge browser verified full-text copying over HTTP, a status
height of two lines, internal overflow and a visible composer at desktop (1280px)
and mobile (390px) widths. Collapsed and expanded sidebars were visually inspected.
Clipboard permission failures produce an actionable error rather than a success label.


## Activity animation and status colors: two iterations

Date: 2026-10-20. Scope: Web activity indicators, calm status colors and SSE recovery.

1. **Iteration 1:** the critic confirmed a **P2** race: a snapshot request already
   in flight could complete after an SSE error and clear the reconnecting status,
   restarting work animation while the event stream was disconnected. A per-connection
   transport flag now prevents snapshot success from clearing that notice until
   an SSE open event and successful synchronization. A regression reproduces the race.
2. **Iteration 2:** the critic confirmed the fix and found no further actionable
   defects. All **25 Web state tests** were independently rerun successfully.

Validation: **25 Web state tests passed**, JavaScript syntax and diff whitespace
checks passed. Real Edge checks covered running/success/error colors, two animation
indicators, reduced-motion disabling, desktop and mobile layout, and both sidebars.
A separate local demo API and worker were started for preview. Screenshots use
controlled sample conversation data and do not represent profiler measurements.
Work animation stops for clarification, terminal states and connection loss.
Green indicates completed execution, never a verified performance improvement.


## Full-document documentation expert

Date: 2026-10-20. Scope: complete Markdown input, documentation-only context,
section URL citations and the separate serialized request budget.

An independent critic found no confirmed blocking or P1/P2 defects. The review
covered request framing in the byte budget, shared execution deadlines, source ID
and URL validation, concise downstream metadata, analyzer/orchestrator compatibility
and the existing locked Markdown dependency. A one-off smoke check verified arbitrary
source link labels, inherited subsection sources, fenced-code exclusion and payload
isolation. Source labels and URLs are appended after the model answer's 16000-character
bound; the integration guide now states that distinction explicitly.

Validation: **152 Python tests passed**, **25 Web state tests passed**, Ruff and
`git diff --check` passed. The package was previously built successfully. New tests
for the full-document feature were removed at the user's request; the six existing
documentation/trigger tests were retained and adapted. Real provider context capacity
still requires deployment validation: byte limits do not count tokens or reserve a
provider-specific output allowance. The expert receives the full document or an
explicit limitation, never a silently truncated excerpt.


## Single dynamic analyzer migration

Date: 2026-10-20. Removed the fixed profiler graph, its analyzer class, the three-method
profiler protocol, response-catalog parser and named gateway wrappers. The only built-in
implementation is now `DynamicProfilerAnalyzer` v2. Synthetic mode supplies a fixture
catalog/data/model to that same graph. Existing v1/legacy active runs must restart;
saved report data remains readable. Custom analyzer factories remain supported.

The planner receives discovered permitted MCP tools, an opaque Result ID and optional
repository/branch metadata. Tokens remain transport-only. It can query the profiler,
select bounded source ranges/symbols from a pinned revision, clarify or finish.
Explicit local paths are scoped without clone/fetch; aliases remain explicit registrations.
Source queries retain five combined excerpts/32 KiB under the common input budget.
The host still enforces authorization, schemas, deadlines, immutable snapshots and
citation checks; planner freedom does not permit arbitrary execution or filesystem access.

Validation: **140 Python tests passed**, **25 Web state tests passed**, Ruff and the
standalone synthetic CLI passed. Obsolete fixed-graph tests were replaced with dynamic
lifecycle tests; token cleanup, clarification correlation, immutable source handling,
source omission/citations and restart coverage were ported. A new source-planning
scenario moves the branch during investigation and confirms the original code is delivered.
The subsequent three-iteration independent review is recorded below. Real private
MCP/model services remain deployment validation work.


## Chat report formatting node

Date: 2026-10-20. Added a durable `format_report` node after interpretation. Its
`ChatReport` contract uses Markdown and at most 16000 characters. Formatting is
deterministic and adds no LLM/tool calls; the executor applies the same boundary
to custom structured reports. Full raw evidence remains in run details rather
than being dumped into chat. Saved reports without `chat_report` remain readable.

Complete fenced JSON is decoded before interpretation/citation validation. Invalid
or free-form model responses are retained in the analyzer checkpoint and shown only
as a bounded unvalidated excerpt with insufficient-data status, not accepted findings.
Formatting removes unsupported markup, preserves source links from metadata, escapes
untrusted text and makes display shortening explicit. Standalone JSON output escapes
Unicode to remain valid through Windows console redirection.

Validation: **145 Python tests passed**, **25 Web state tests passed**, Ruff and
`git diff --check` passed. New checks cover fenced JSON, free-form HTML, invalid
citations, output bounds, custom presentation normalization and worker reconstruction
at the formatting checkpoint without repeating interpretation. The synthetic CLI
produced a formatted report; the built wheel contains the new formatter and excludes
the removed fixed-analyzer modules. The subsequent independent review is recorded below.

## Three-iteration dynamic analyzer and report review

Date: 2026-10-20. Scope: the complete migration and report-formatting change above.

Iteration 1 identified two defects. Planner-selected source could replace required
user-selected locations and inherit user-selection provenance. The report node now
collects the original required locations independently, verifies their availability
and prioritizes them before adding bounded model-selected context. Model selections
carry separate origin/resolution values. Host-generated analysis-input questions also
lacked a durable question entry, causing subsequent answers to lose or shift context.
All issued analysis-input questions now enter the durable question journal.

Iteration 2 confirmed both fixes and found no additional actionable defects. The
critic independently ran 71 dynamic-analysis, report-formatting and source tests.
Iteration 3 rechecked custom analyzer formatting, checkpoint recovery, MCP schema
and configuration enforcement, token isolation, required source constraints and
deadline handling; no additional actionable defects were found.

Final validation: **148 Python tests passed**, **25 Web state tests passed**, Ruff
and `git diff --check` passed. Exported contract schemas and integration guidance
were updated. Real private MCP servers and model providers still require deployment
validation; these local checks do not establish compatibility with their private schemas.

## Provider-neutral Git MCP and personal Bearer tokens

Date: 2026-10-20. Local implementation validation followed by independent critic review.

Profiler and Git gateways now share bounded MCP discovery/schema/transport code.
The analyzer namespaces tools by server, checkpoints separate catalogs/fingerprints
and shares the investigation/model budgets. Repository-only runs no longer need a
profiler ID when Git MCP is configured. Custom factories receive `AnalysisServices.git`
without requiring built-in analyzer attributes. The built-in worker no longer selects
GitLab REST; the standalone legacy module remains import-compatible.

Git endpoint/allowlist/timeout and optional fallback token are configured through ENV.
Personal Git tokens use Project settings and the existing encrypted credential flow,
with `Authorization: Bearer ...` sent only to Git MCP. The separate `git_mcp` credential
kind binds them to both endpoint and repository. Saved personal tokens take precedence
over ENV fallback. Rotation/removal preserves active-run references; unused records
are pruned normally. Legacy GitLab credentials are never automatically forwarded.

Validation: **170 Python tests passed**, **26 Web state tests passed**, Ruff and
`git diff --check` passed. Tests cover equal tool names on two servers, repository-only
runs/reconstruction, custom analyzer factories, schema/policy drift, deadlines, encrypted
personal-token snapshots, destination binding, ENV fallback and cleanup. A live local
Streamable HTTP MCP fixture confirmed discovery, schema-driven calls and Bearer auth.
Browser checks covered generic repository saves, personal-token input clearing,
collapsed panels and the narrow-screen overlay. Nine Markdown files, 31 local links/
anchors and four Python snippets passed documentation checks. Contract schemas updated.

Generic Git MCP schemas cannot establish repository scope, immutable commit/blob
identity or profile-build linkage. These remain explicit report limitations; MCP data
does not replace required host-verified local ranges. Server-side permissions must
enforce access scope. Private Git MCP deployments and real LLM planning still require
deployment validation. ENV fallback credentials are process configuration rather than
immutable per-run snapshots; the snapshot guarantees above apply to saved personal tokens.

The independent critic reproduced one P2: a repository-only run skips profiler
discovery when its Result ID is empty. A subsequent Result ID answer is retained
only as clarification context; the profiler catalog stays empty, and a profiler
call fails with `insufficient_data`. On 2026-10-20 the user explicitly chose to
retain this behavior and publish the implementation. Workaround: start a new run
with Profile supplied. No fix or regression coverage for that transition is claimed.
The critic found no other confirmed defects and independently passed 31 Git MCP/
credential tests. The main review reran 71 focused Python tests, 26 Web tests, Ruff
and documentation checks successfully.

## Versioned analyzer context: plan and implementation review

Date: 2026-10-20. The user explicitly authorized replacing the previously retained
Git-only to profiler restriction. The reviewed plan requirements are summarized
below; the current public lifecycle is in [analyzer-context.md](docs/analyzer-context.md).
The completed standalone plan was removed to avoid duplicating this review and guide.

The plan critic required nondestructive catalog refresh, explicit replace/compare
evidence scope, durable acceptance before side effects, strict patch/null semantics,
separate confirmation/Q&A journals, immutable credential bindings and opt-in custom
analyzer compatibility. These requirements are implemented. Original run tasks stay
immutable; accepted contexts have an ordered host ledger and evidence provenance.

Code review found two P2 defects, both fixed and regression-tested:

- Empty catalog discovery incorrectly retained a previous catalog. The graph now
  distinguishes inactive services from actual discovery, compares empty results too,
  and rejects any change to a previously discovered pinned catalog.
- Clearing Result ID without Git MCP re-entered legacy bootstrap outside the ledger.
  Proposals leaving no available analysis entrypoint are rejected both before showing
  confirmation and when accepting it; the original context and answer remain available.

The final critic review confirmed both fixes and found no additional confirmed
runtime, checkpoint, scope or credential defects. Validation: **191 Python tests**,
**26 Web tests**, Ruff and `git diff --check` passed. Tests cover Git-only to profiler,
replace/compare scoped citations, confirmation replay/staleness/decline, cumulative
budgets, endpoint/catalog drift, required source re-selection, unchanged-commit branch
changes, personal token destination binding and old/new custom analyzer signatures.
Documentation links and Python examples passed the documentation audit; exported
schemas were updated alongside their source contracts.

No real provider/LLM context-update deployment was exercised. Generic Git MCP responses
still cannot certify repository scope, commit/blob identity or profile-build linkage.
The synchronous analyze() convenience path returns needs-input for a proposed update;
durable worker/step_analysis integration is required to confirm and resume it.

## Worker and analyzer bridge readability refactor

Date: 2026-10-21. Scope: two implementation modules; public contracts and saved
checkpoint formats are unchanged.

Self-review identified mixed responsibilities in Worker.tick(): job dispatch,
intake, cancellation, executor invocation and deadline enforcement were nested in
one method. Private intake and execution handlers now keep dispatch/error handling
separate from each job lifecycle. An unnecessary local source-gateway alias was
also removed.

The analyzer bridge embedded context-proposal validation and confirmation creation
inside its main step method. A private proposal handler now groups that transition
next to acceptance handling. It preserves opt-in checks, answer correlation,
rejection feedback, checkpoint updates and the separate durable acceptance step.

Manual diff review checked call ordering, exception handling, late-result rejection,
legacy analyze()/step_analysis signatures and unchanged cursor fields. No independent
critic review was performed for this refactor. Existing regression coverage was
used; no new behavior or uncovered behavioral defect was introduced intentionally.

Validation: 191 Python tests passed, including dynamic analysis, Git MCP, context
updates, credentials, source and report formatting; all 26 Web state tests passed.
Ruff (src, tests, scripts) and git diff --check passed. Real MCP/LLM deployments
were not exercised. The analyzer step still owns substantial request/source
validation; broader restructuring is deferred to keep this change reviewable.

### Follow-up: clarification replies and terminal reports

Date: 2026-10-21. A second bounded pass separates clarification reply application
and terminal report formatting from ProfileAnalysisExecutor.step(). Reply parsing
now uses early returns for plain-text Result IDs and analyzer answers, followed by
explicit branches for options, source patches and individual JSON fields. This
removes nested source-type checks while preserving shallow source merging and the
special policy=off replacement rule. Invalid replies still checkpoint a new
clarification with the same question text and retry-stable ID calculation.

Terminal formatting and event/cursor construction now share one private method;
needs-input reports remain in the clarification path. Unvalidated output handling,
result status precedence, full report persistence and custom analyzer invocation
are unchanged. Manual review compared these branches with the previous code.
No independent critic review or real MCP/LLM deployment was performed.

Validation rerun: 191 Python tests, 26 Web state tests, Ruff (src, tests, scripts)
and git diff --check passed. Existing tests cover malformed options, corrected
Result IDs, source disabling, context answer journals and report reconstruction.
No new test was added because this pass did not identify an uncovered behavioral
defect. Request/source validation remains in step(); public contracts, schemas
and architecture documentation require no changes for these private extractions.

## Common backend consultation experts

Date: 2026-10-21. Implemented a backend-only ExpertRegistry, typed requests/responses,
input schemas, explicit ContextPolicy and trusted factories. Worker accepts experts=;
default_experts() registers ProfilerDocsAgent through the same boundary as extensions.
No registration UI, executable uploads or new deployment environment variables were added.

Routing precedes extraction and distinguishes task, consultation and mixed requests.
Consultations preserve the complete specification and version. Mixed requests require
explicit task-change intent. One expert is called per turn, under the shared intake
deadline and bounded input/context/result sizes. Unknown experts, malformed responses,
router failures and late expert results produce non-echoing limitations.

The user requested removing the documentation expert's special path. ProfilerDocsAgent
now directly implements ask(ExpertRequest); no adapter, documentation-specific graph
node/output or orchestration branch remains. LLM routing uses the common catalog and
does not require profiler keywords. Optional matchers, including triggers.txt, are
demo shortcuts only. Original ask(question, version, platform), as_tool(), document
validation and analysis-time lookups remain compatible and covered by tests.

Self-review identified and addressed these behavioral risks:

- Leaving consultation messages in subsequent extraction could turn reference questions
  or expert claims into task agreements. Store now records their indices and excludes
  them from TaskAgent input; mixed assistant replies retain only their task portion.
- In-memory pending questions would lose expert selection on Worker restart. Internal
  intake_turns records commit selection, result and pending question atomically with
  the reply and job completion. Lease checks suppress stale/duplicate commits.
- Expert context could expose unnecessary task/history or mutate host state. Definitions
  deny both by default, and explicitly permitted context is copied and size checked.
- Invalid reconstructed response objects and whitespace-only answers could bypass useful
  response checks. Common responses revalidate instances and reject blank text.

Validation: 219 Python tests and 26 Web state tests passed. Ruff (src, tests, scripts),
git diff --check, contract export, local documentation links and executable registry
examples passed. New coverage includes routing/extraction separation, mixed replies,
restart and replay, removed experts, custom schemas, source URL checks, context copies,
size/deadline failures, private-error suppression and direct documentation compatibility.
AGENTS.md, README and the integration/consultation guides describe the final design.

Initial implementation validation did not include an independent critic; the subsequent
three-iteration review is recorded below. No real provider/MCP deployment was performed.
LLM intent classification can still be wrong; explicit /ask and /task messages avoid that choice.
Trusted synchronous experts must implement bounded transports: the host rejects late
results but cannot forcibly stop arbitrary Python. Older unclassified chat messages
keep their prior interpretation; no historical task or conversation migration is claimed.

### Three critic iterations

Date: 2026-10-21. At the user's request, a separate read-only critic reviewed the
implementation in three consecutive iterations. The main agent implemented fixes
between iterations; no commits or pushes were made.

1. **Iteration 1:** two confirmed P2 findings, both reproduced and fixed.
   - Revalidating ExpertResponse did not revalidate an already constructed nested
     ExpertSource. An unsafe URL could pass the registry and then fail graph
     rendering instead of producing unavailable. ExpertSource now revalidates
     instances too; a graph-level regression verifies rejection without task changes.
   - A temporary router/expert failure cleared the pending question. Retrying a
     short reply after restart could enter task extraction. Pending consultation is
     now retained on unavailable/routing failures and cleared on completion or
     explicit task/expert switching. Regressions cover both failure sources and
     restart/retry. Main-agent focused validation: 37 tests and Ruff passed.
2. **Iteration 2:** the critic verified the first fixes and independently passed
   37 focused tests. Two additional P2 findings were reproduced and fixed.
   - Worker accepted a graph result returned after the intake deadline. A final
     check_deadline() now runs inside the deadline scope before persistence; a
     regression proves neither late task updates nor late replies are committed.
   - The limitations item-length bound existed only in a Python validator and
     was absent from the JSON schema. It is now a declarative Annotated string
     constraint; schemas were regenerated and a boundary/schema regression added.
     Main-agent focused validation: 39 tests and Ruff passed.
3. **Iteration 3:** the critic found no new confirmed runtime or compatibility
   defects. It independently passed 39 focused tests, matched all three exported
   expert schemas to model_json_schema(), and passed git diff --check. It checked
   deadline enforcement, nested sources, pending durability, task-history isolation,
   Store changes and the direct documentation ask()/as_tool() APIs.

Final main-agent validation: **224 Python tests**, **26 Web state tests**, Ruff
(src, tests, scripts), contract export and git diff --check passed. The LF/CRLF
notice for README is informational. The remaining deployment/LLM-classification
and synchronous transport limitations above still apply; no real provider test
is claimed by either reviewer.

### Two-critic consensus before publication

Date: 2026-10-21. At the user's request, two separate read-only critics reviewed
the complete pending feature, exchanged findings and independently reproduced each
other's scenarios. They agreed on two additional P2 defects:

- Custom input models were validated, dumped and then revalidated. Aliases could
  change names during serialization, so schema-valid inputs never reached the
  expert. ExpertRequest now receives validated.model_copy(deep=True); JSON dumps
  serve only budget/context checks. Regressions cover ordinary aliases, different
  validation/serialization aliases, AliasPath and computed fields.
- Rejecting an expired consultation through the generic job-failure path left its
  messages unclassified. A later task request could import the earlier /ask or
  clarification reply into task notes. Pure consultation expiry now discards all
  generated data and commits only a host-owned unavailable reply and classification
  under the existing lease check. Task/mixed expiry still rejects task/reply changes.

Their verification found an intermediate regression in the initial expiry fix:
unconditionally restoring the previous pending question could revive an old expert
after explicit selection timed out. A shared retain_pending() rule now governs both
normal routing and expiry. It retains only continuing consultations and clears old
questions on explicit same/different /ask or expert-ID switching; late expert pending
state is never accepted. Tests cover timeout switching and continued questions across
restart, plus exclusion from later task intake.

Both critics approved the corrected diff by consensus, found no additional confirmed
defects and independently passed **47 focused tests** and git diff --check. Lease
ownership and stale/duplicate commit guards remain in Store.finish_intake. The main
agent passed **232 Python tests**, **26 Web state tests**, Ruff, contract export,
schema consistency and documentation checks. AGENTS.md and the extension guide now
document typed-input copying, expiry classification and pending-selection rules.
No real provider/MCP compatibility validation is claimed.

## Documentation consolidation and generated-file cleanup

Date: 2026-10-21. Removed the completed source-context plan and standalone deployment
review after retaining their historical conclusions here. Current behavior belongs
to the integration, analyzer, context-update and deployment guides; the original
documents remain available in Git history.

- The source-context design's four critique iterations established typed provenance,
  read-only Git access, a durable immutable freeze point, distinct identity/symbol
  uncertainty, required user-selected evidence, a shared citation/input budget and
  persisted unavailable/disabled outcomes. Its fixed metric/hotspot flow and rule
  requiring a new run for every Result ID change were superseded by dynamic planning
  and confirmed versioned context updates. Trusted build-manifest linkage remains
  future work; no such deployment validation is claimed.
- The 2026-10-18 deployment review's two iterations addressed configurable listeners,
  portable setup, worker verification, encryption prerequisites, HTTPS examples and
  dependency coverage. That historical report recorded a fresh Windows Python 3.12
  installation, 97 Python tests, eight Web tests, Ruff/dependency checks and a live
  demo API/worker workflow with a temporary database. Linux/macOS and real private
  services were not exercised. These are historical results, not new checks.

Removed the integration guide's duplicate README/setup/demo sections and obsolete
intake diagram. Persistence notes now distinguish full routing history from filtered
task input and consultation version preservation. README links to the owning guides;
setup examples preserve existing .env files. Analyzer documentation now distinguishes
direct scope changes from host-confirmed context proposals. No runtime code changed.

Cleanup targets are only the obsolete documentation and reproducible build/pytest/
Ruff caches. Local configuration, virtual environment, data, compatibility modules,
schemas, licenses and knowledge-base files are preserved. Removed two historical
documents and 67 generated files (51 build files, five pytest cache files and eleven
Ruff cache files), after checking all resolved targets remain under the workspace
and contain no reparse points. Validation passed for nine documents, 44 local
links/anchors, six Python examples and git diff --check. No runtime suite was run
for these documentation/generated-file-only changes; no new critic or deployment
validation is claimed.

Pre-publication recheck on 2026-10-21 passed the same document/link/Python checks
and confirmed that the diff contains only Markdown changes and the two intended
document deletions. All ten PowerShell fenced examples parsed successfully.
The documented .env guard created a missing file and preserved an existing file
in an isolated temporary workspace directory, which was removed afterward.
