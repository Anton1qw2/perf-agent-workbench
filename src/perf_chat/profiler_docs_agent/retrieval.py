"""Full-document snapshots and section source links; no retrieval index."""

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from pydantic import BaseModel


class DocumentationSource(BaseModel):
    id: str
    file: str
    title: str
    url: str | None = None
    sha256: str


def _source_link(tokens, index):
    if index + 2 >= len(tokens) or tokens[index].type != "paragraph_open":
        return None
    inline = tokens[index + 1]
    first_line = inline.content.split("\n", 1)[0].strip()
    if inline.type != "inline" or len(first_line) > 16384:
        return None
    children = MarkdownIt().parseInline(first_line)[0].children or []
    if (inline.type != "inline" or not children or children[0].type != "link_open"
            or children[-1].type != "link_close"
            or sum(t.type == "link_open" for t in children) != 1):
        return None
    url = children[0].attrGet("href") or ""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password):
            return url
    except ValueError:
        pass
    return None


def document_sources(content: str, filename: str, digest: str) -> list[DocumentationSource]:
    parser = MarkdownIt()
    tokens = []
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
    parser.block.parse(normalized, parser, {}, tokens)
    sources = [DocumentationSource(
        id="doc0", file=filename, title=filename, sha256=digest,
        url=_source_link(tokens, 0),
    )]
    parents = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        level = int(token.tag[1:])
        while parents and parents[-1][0] >= level:
            parents.pop()
        inline = tokens[index + 1]
        title = inline.content
        inherited = parents[-1][1].url if parents else sources[0].url
        source = DocumentationSource(
            id=f"doc{len(sources)}", file=filename, title=title, sha256=digest,
            url=_source_link(tokens, index + 3) or inherited,
        )
        sources.append(source)
        parents.append((level, source))
    return sources


def load_document(path: Path, max_bytes: int):
    """Read one immutable UTF-8 snapshot; reject oversized input instead of truncating."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError:
        return "missing", "", []
    except OSError:
        return "unavailable", "", []
    if len(raw) > max_bytes:
        return "too_large", "", []
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "unavailable", "", []
    if not content.strip():
        return "empty", "", []
    digest = hashlib.sha256(raw).hexdigest()
    return "found", content, document_sources(content, path.name, digest)
