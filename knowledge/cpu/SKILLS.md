# CPU function costs

## Applicability
Functions or call paths with server-reported CPU-related cost relevant to the goal.
The metric's meaning must be established by the profiler, not inferred from its name.

## Hypotheses
A high reported function cost may require distinguishing costly individual calls
from frequent calls. Inclusive and exclusive costs may describe different scopes.

## Checks
Inspect the available call-count, stack and cost evidence when their semantics are
documented. Preserve caller/callee relationships and avoid adding overlapping costs.
Request function-scoped details only when a permitted tool supports them.

## Limitations
Missing call counts or undocumented cost semantics prevent a conclusion about which
explanation applies. Source resemblance alone does not identify a runtime hotspot.
