"""Structural evidence checks; semantic interpretation still requires review."""

from .contracts import Interpretation


def validate_interpretation(result: Interpretation, evidence_ids: set[str]):
    for item in result.observations + result.hypotheses:
        if not set(item.evidence_ids) <= evidence_ids:
            raise ValueError("Interpretation references unknown evidence")
    return result
