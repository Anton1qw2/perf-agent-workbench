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
