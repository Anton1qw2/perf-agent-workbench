"""Structural evidence checks; semantic interpretation still requires review."""

from .contracts import Interpretation


def metric_names(payload) -> list[str]:
    # Server response schema is pending; support two documented, explicit common forms.
    items = payload.get("metrics") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("Expected a metric list or an object with a metrics list")
    names = []
    for item in items:
        name = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each metric must be a name or an object with a string id")
        if name not in names:
            names.append(name)
    return names


def validate_interpretation(result: Interpretation, evidence_ids: set[str]):
    for item in result.observations + result.hypotheses:
        if not set(item.evidence_ids) <= evidence_ids:
            raise ValueError("Interpretation references unknown evidence")
    return result
