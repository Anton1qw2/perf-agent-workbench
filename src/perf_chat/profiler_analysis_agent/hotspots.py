"""Hotspot contracts and exact, bounded projections of immutable MCP evidence."""

import copy
import json
import re

from .contracts import HotspotSpec

MAX_HOTSPOTS = 8
MAX_HOTSPOT_BYTES = 24_000
MAX_HOTSPOT_CALLS = 2


def usable(value):
    return value is not None and not (
        isinstance(value, str) and not value.strip()
        or isinstance(value, (list, dict)) and not value
    )


def resolve_pointer(value, pointer):
    if not pointer:
        return value
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid evidence JSON pointer")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("Invalid evidence array index")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("Evidence pointer does not identify a value")
    return value


def project_evidence(spec: HotspotSpec, evidence: list[dict]):
    """Preserve selected values and provenance; never summarize or calculate metrics."""
    available = {item["id"]: item for item in evidence}
    grouped = {}
    try:
        for selection in spec.selections:
            original = available[selection.evidence_id]
            value = resolve_pointer(original["response"], selection.pointer)
            if not usable(value):
                raise ValueError("Hotspot selection contains no usable evidence")
            item = grouped.setdefault(original["id"], {
                **{k: copy.deepcopy(v) for k, v in original.items() if k != "response"},
                "response": [],
            })
            item["response"].append({"pointer": selection.pointer, "value": copy.deepcopy(value)})
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Hotspot selection must reference existing evidence") from exc
    result = list(grouped.values())
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_HOTSPOT_BYTES:
        raise ValueError("Hotspot evidence selection exceeds its context budget")
    return result
