"""Shared context ledger validation; never reads analyzer-owned checkpoints."""

from .context_contracts import AnalysisContext, AnalysisContextUpdate


def effective_context(task, cursor):
    legacy_id = cursor.get("result_id", task.target.profile or "")
    if not isinstance(legacy_id, str) or len(legacy_id) > 4096:
        # An invalid legacy clarification must remain correctable on the next reply.
        legacy_id = task.target.profile or ""
        if len(legacy_id) > 4096:
            legacy_id = ""
    context = AnalysisContext.model_validate(cursor.get("analysis_context_initial") or {
        "result_id": legacy_id,
        "repository": task.target.repository, "branch": task.target.branch,
    })
    seen = set()
    updates = [AnalysisContextUpdate.model_validate(item) for item in cursor.get("context_updates", [])]
    if len(updates) > 8:
        raise ValueError("Context update limit reached")
    for update in updates:
        if (update.previous != context or update.current.revision != context.revision + 1
                or update.update_id in seen):
            raise ValueError("Invalid context ledger")
        seen.add(update.update_id)
        context = update.current
    return context, updates
