# Allocation-related investigations

## Applicability
Evidence explicitly associated with allocations, object lifetime or memory use.

## Hypotheses
Frequent temporary allocation is a possible explanation only when relevant evidence
supports investigating it. CPU samples alone do not establish allocation volume.

## Checks
Inspect server-provided allocation counts, sizes and relevant call stacks if available.
Before recommending reuse, inspect applicable ownership and lifetime constraints.
Describe a repeatable workload and measurements for validating a proposed change.

## Limitations
Do not infer missing sizes, allocation counts, units or improvement percentages.
An allocation-related recommendation remains unvalidated until separately measured.
