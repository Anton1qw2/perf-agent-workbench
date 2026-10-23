# Analyzer implementation and refactoring review

## Analyzer hotspot graph and local knowledge — 2026-10-23

Implemented the v5 durable hotspot queue behind the existing `ProfileAnalyzer`
interface, with exact evidence projections, local investigation, validated findings
and cross-hotspot synthesis. Added an optional `SKILLS.md` directory snapshot through
`PERF_CHAT_KNOWLEDGE_ROOT` and the explicit `AnalysisServices.knowledge` dependency.
The queue is sequential; no concurrent mutation of one run cursor was introduced.
Existing unrelated working-tree changes were preserved.

Findings from implementation and regression review:

- The shared strict model stripped document whitespace. Knowledge documents now
  preserve exact decoded content so their hashes and resumed model input agree.
- Windows `DirEntry.stat()` reports zero device/inode identifiers. File identity
  checks now compare `Path.lstat()` with the opened descriptor before reading.
- Required user source ranges must be collected before each hotspot's interpretation,
  not only before final reporting. The existing preparation logic is shared by both
  paths, and required evidence remains mandatory in synthesis.
- Adding hotspot schemas/instructions exceeded the small input budgets in two source
  regressions. Removed repeated planner instructions without increasing those budgets
  or dropping policy/source rules; both regression tests pass unchanged.
- Oversized local responses now produce a partial hotspot result while retaining raw
  evidence. They are not delivered to another hotspot or silently truncated.
- Invalid knowledge branch selection falls back to root guidance with a limitation;
  knowledge paths cannot serve as measurement citations. Snapshots remain unchanged
  across restarts, file edits and dependency reconstruction.

Validation:

- `python -m pytest -q --basetemp=data/analyzer-knowledge-full-20261023-02 --tb=short`:
  343 passed, 1 skipped. The skipped test requires symlink creation permission on Windows.
- `python -m ruff check src tests scripts`: passed.
- `node --test tests/web_state.test.cjs`: 31 passed.
- `python scripts/export_contracts.py`: regenerated `ProfileAnalysis` and `AnalysisStep`
  schemas from models; older saved reports retain optional-field defaults.
- `git diff --check`: passed.

These checks use local fixtures. No live profiler/Git MCP or Confluence connectivity,
LLM provider compatibility or measured analysis-quality improvement was established.
Old active analyzer checkpoints require a new run; saved reports remain readable.

### Delegated analyzer refactoring

A separate refactoring agent reviewed only the analyzer interfaces and implementation.
Consolidated interpretation-phase selection and usable-evidence checks, and replaced
the remaining literal hotspot-call limit with the owning constant. The review found
that exceeding the total input budget in hotspot planning terminated the whole run;
it now records a partial hotspot result and continues the queue. Required-source
clarifications, deadline failures and policy validation retain their original boundaries.
Added regression coverage for the budget failure and all three failure boundaries.

- Agent verification: hotspot tests 26 passed, 1 skipped; targeted Ruff passed.
- Parent verification after refactoring: full Python suite 347 passed, 1 skipped;
  full Ruff passed; Web tests 31 passed.
- Earlier unrelated local source/test edits are excluded from the analyzer commit.
- Verification of the exact staged snapshot, independently of those local edits:
  Python 333 passed, 1 skipped; Ruff of changed Python modules/tests passed;
  Web tests 31 passed. The first snapshot test invocation lacked the parent directory
  for its relative pytest temp path; the successful run used an absolute workspace
  temp path. No application change was needed for that test setup error.
