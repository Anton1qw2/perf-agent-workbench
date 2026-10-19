"""Stable evidence records for arbitrary MCP operations."""

import hashlib
import json
from datetime import datetime, timezone

from .contracts import Evidence


def evidence(operation, arguments, response, index):
    digest = hashlib.sha256(
        json.dumps(
            {"operation": operation, "arguments": arguments, "response": response},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return Evidence(
        id=f"e{index}",
        operation=operation,
        arguments=arguments,
        response=response,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        sha256=digest,
    ).model_dump()

