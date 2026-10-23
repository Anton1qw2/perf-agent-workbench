"""One byte budget and citation registry for the actual model request."""

import json
from dataclasses import dataclass


class ContextBudgetExceeded(ValueError):
    pass


class RequiredSourceOmitted(ValueError):
    pass


@dataclass
class ModelContext:
    payload: dict
    evidence_ids: set[str]
    limitations: list[str]


class PromptContextBuilder:
    def __init__(self, max_bytes=120_000, *, token_counter=None, max_tokens=None):
        self.max_bytes = max_bytes
        self.token_counter = token_counter
        self.max_tokens = max_tokens
        if (token_counter is None) != (max_tokens is None):
            raise ValueError("Provide both a provider-specific token counter and token budget")

    def build(self, model, system, payload, source_context, *, required=False):
        payload = dict(payload)
        sources = [s.model_dump() for s in source_context.evidence] if source_context else []
        payload["source_evidence"] = sources
        payload["source_snapshot"] = (
            source_context.snapshot.model_dump()
            if source_context and source_context.snapshot
            else None
        )
        notes = []

        def fits():
            if hasattr(model, "build_request"):
                body = model.build_request(system, payload, json_output=True)
            else:
                body = {
                    "model": "",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                }
            within_bytes = (
                len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= self.max_bytes
            )
            return within_bytes and (
                self.token_counter is None or self.token_counter(body) <= self.max_tokens
            )

        if not fits() and payload.get("knowledge"):
            payload["knowledge"] = []
            notes.append("Knowledge documents omitted from model context due to input budget.")
        if not fits() and not required and sources:
            payload["source_evidence"] = []
            notes.append("Optional source excerpts omitted from model context due to input budget.")
        if not fits() and payload.get("documentation"):
            payload["documentation"] = None
            notes.append("Documentation omitted from model context due to input budget.")
        if not fits() and required:
            payload["source_evidence"] = []
            if fits():
                raise RequiredSourceOmitted(
                    "Select a smaller required source range or explicitly continue without source."
                )
        if not fits():
            raise ContextBudgetExceeded(
                "Essential metric evidence is too large for the total model input budget; raw evidence retained."
            )
        ids = {e["id"] for e in payload["evidence"]} | {s["id"] for s in payload["source_evidence"]}
        if required and not any(
            s["resolution"] == "provided_location" for s in payload["source_evidence"]
        ):
            raise RequiredSourceOmitted(
                "Select an explicit source path and range for model interpretation."
            )
        return ModelContext(payload, ids, notes)
