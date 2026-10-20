# Code review notes

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
