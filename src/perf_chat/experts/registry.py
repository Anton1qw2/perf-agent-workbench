"""Registration is trusted backend code; descriptions do not grant tool access."""

import json
import re
from collections.abc import Callable

from ..runtime import check_deadline, execution_deadline
from .contracts import ExpertDefinition, ExpertRequest, ExpertResponse, ExpertServices


def check_size(value, max_bytes):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > max_bytes:
        raise ValueError("Consultation exceeds the model input budget")


class ExpertRegistry:
    def __init__(self):
        self._entries = {}

    def register(self, definition: ExpertDefinition, factory: Callable, *, matcher=None):
        if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", definition.id)
                or definition.id in self._entries):
            raise ValueError("Expert IDs must be unique lowercase identifiers")
        if not definition.name.strip() or not definition.description.strip():
            raise ValueError("Expert name and description are required")
        if len(self._entries) >= 32:
            raise ValueError("At most 32 consultation experts can be registered")
        self._entries[definition.id] = (definition, factory, matcher)

    def catalog(self):
        return [
            {"id": definition.id, "name": definition.name,
             "description": definition.description,
             "input_schema": definition.input_schema.model_json_schema()}
            for definition, _, _ in self._entries.values()
        ]

    def match(self, question):
        return next((definition.id for definition, _, matcher in self._entries.values()
                     if matcher is not None and matcher(question)), None)

    def consult(self, expert_id, inputs, *, services=None, messages=(), task=None,
                deadline=None, max_bytes=120000):
        try:
            definition, factory, _ = self._entries[expert_id]
            check_size(inputs, max_bytes)
            validated = definition.input_schema.model_validate(inputs)
            policy = definition.context_policy
            # Copy only permitted data; experts cannot mutate the host's task/history.
            context = json.loads(json.dumps({
                "inputs": validated.model_dump(mode="json"),
                "history": list(messages) if policy.conversation_history else [],
                "task": task if policy.task else None,
            }))
            check_size(context, max_bytes)
            with execution_deadline(deadline):
                expert = factory(services or ExpertServices())
                response = ExpertResponse.model_validate(expert.ask(ExpertRequest(
                    inputs=validated.model_copy(deep=True),
                    history=tuple(context["history"]), task=context["task"], deadline=deadline,
                )))
                check_deadline()
            if response.status == "needs_input" and not response.question:
                raise ValueError("An expert clarification requires a question")
            check_size(response.model_dump(mode="json"), max_bytes)
            return response
        except Exception:
            # Providers and validation errors can contain credentials or private payloads.
            return ExpertResponse(status="unavailable", answer=(
                "The consultation expert is unavailable or returned an invalid response. "
                "No guidance was accepted."
            ))
