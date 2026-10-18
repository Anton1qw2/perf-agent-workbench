"""Bounded lexical search over a local Markdown knowledge base; no external index."""

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel


class DocumentationSource(BaseModel):
    file: str
    start_line: int
    end_line: int
    text: str
    sha256: str


STOP_WORDS = {
    "the", "a", "an", "and", "or", "is", "are", "to", "for", "of", "in", "on",
    "how", "what", "can", "i", "do", "with", "perf", "intel", "profiler",
}


def terms(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.casefold())) - STOP_WORDS


def search_document(path: Path, question: str) -> tuple[str, list[DocumentationSource]]:
    # Read each time so edits become available without restarting or rebuilding an index.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "missing", []
    except OSError:
        return "unavailable", []
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "unavailable", []
    if not content.strip():
        return "empty", []
    query = terms(question)
    if not query:
        return "not_found", []
    lines = content.splitlines()
    digest = hashlib.sha256(raw).hexdigest()
    candidates = []
    # Overlap keeps commands next to their explanations; at most three bounded excerpts.
    for start in range(0, len(lines), 24):
        text = "\n".join(lines[start:start + 32])
        score = len(query & terms(text))
        if score:
            candidates.append((score, start, text))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    sources = []
    for _, start, text in candidates[:3]:
        # Limit unusually long lines as well as the number of lines.
        excerpt = text[:6000]
        sources.append(DocumentationSource(
            file=path.name, start_line=start + 1,
            end_line=start + len(excerpt.splitlines()), text=excerpt, sha256=digest,
        ))
    return ("found" if sources else "not_found"), sources
