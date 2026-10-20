"""Deterministic, bounded Markdown for Web and TUI; never creates analysis facts."""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from .contracts import ChatReport

MAX_CHAT_CHARACTERS = 16000
_MARKDOWN = MarkdownIt("commonmark")


def parse_analysis_json(text):
    """Accept JSON or one complete Markdown JSON fence, without extracting partial JSON."""
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        raise ValueError("Analyzer output must be text or JSON")
    value = text.strip()
    match = re.fullmatch(r"(`{3,}|~{3,})(?:json)?[ \t]*\r?\n(.*?)\r?\n\1", value, re.S | re.I)
    if match:
        value = match.group(2)
    return json.loads(value)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "think", "analysis"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "think", "analysis"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    """Reduce arbitrary report markup to text without fetching links or running code."""
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", value)
    parts = []
    hidden = 0
    for token in _MARKDOWN.parse(value):
        if token.type in {"fence", "code_block"}:
            parts.extend(["\n", token.content, "\n"])
        elif token.type == "html_block":
            parser = _HTMLText()
            parser.feed(token.content)
            parts.extend(parser.parts)
        elif token.type == "inline":
            for child in token.children or []:
                if child.type == "html_inline":
                    name = re.match(r"</?(script|style|think|analysis)\b", child.content, re.I)
                    if name:
                        if child.content.startswith("</"):
                            hidden = max(0, hidden - 1)
                        else:
                            hidden += 1
                elif not hidden and child.type in {"text", "code_inline", "image"}:
                    parts.append(child.content)
                elif not hidden and child.type in {"softbreak", "hardbreak"}:
                    parts.append("\n")
            parts.append("\n")
    return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())


def _escape(value, limit=1500):
    raw = str(value)
    input_limit = max(4096, limit * 2)
    text = " ".join(plain_text(raw[:input_limit]).split())
    if len(text) > limit or len(raw) > input_limit:
        text = text[:limit] + "…"
    # HTML is escaped explicitly because links and raw tags have no place in report fields.
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*{}_\[\]()#!|])", r"\\\1", text)


def _safe_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {"https", "http"} and parsed.hostname
                and not parsed.username and not parsed.password
                and not re.search(r"[<>\s\x00-\x1f]", url))
    except ValueError:
        return False


def format_chat_report(report, *, unvalidated_output=None):
    """Render validated findings separately from any unvalidated model output."""
    title = "## Performance analysis" + (" — SYNTHETIC DEMO" if report.demo else "")
    status = {"completed": "Analysis completed", "needs_input": "More information needed",
              "insufficient_data": "Insufficient validated data"}[report.status]
    blocks = [title, f"**Status:** {status}\n\n**Result ID:** {_escape(report.request.result_id or 'Not supplied', 500)}"]
    if report.demo:
        blocks.append("**Synthetic data only. No real measurements or optimization were established.**")
    shortened = False
    footer = "Full structured results, profiler responses and source evidence are retained in the run details."

    def add(block):
        nonlocal shortened
        if sum(len(item) + 2 for item in blocks) + len(block) + len(footer) + 150 > MAX_CHAT_CHARACTERS:
            shortened = True
            return False
        blocks.append(block)
        return True

    def section(title, items):
        nonlocal shortened
        lines = ["- " + item for item in items[:12] if item]
        if len(items) > 12:
            shortened = True
        while lines:
            block = "### " + title + "\n\n" + "\n".join(lines)
            if add(block):
                break
            lines.pop()

    for heading, findings in [("Observations", report.observations), ("Hypotheses", report.hypotheses)]:
        section(heading, [f"{_escape(item.text)} ({', '.join(_escape(eid, 100) for eid in item.evidence_ids)})"
                          for item in findings])
    if not report.observations:
        add("No validated observations were produced.")
    section("Recommended checks", [_escape(text) for text in report.recommendations])
    section("Limitations", [_escape(text) for text in dict.fromkeys(report.limitations)])
    if unvalidated_output:
        raw = str(unvalidated_output)
        text = plain_text(raw[:8000])
        excerpt = text[:4000] or "No displayable text remained after markup normalization."
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", excerpt)), default=0) + 1)
        add("### Unvalidated analyzer response\n\n"
            "This response did not pass the analysis contract or citation checks; it is not a validated finding.\n\n"
            + fence + "text\n" + excerpt + "\n" + fence)
        if len(text) > len(excerpt) or len(raw) > 8000:
            shortened = True
    section("Evidence references", [f"{_escape(item.id, 100)} — {item.server}: {_escape(item.operation, 256)}"
                                     for item in report.evidence[:12]])
    if report.source_context and report.source_context.snapshot:
        snapshot = report.source_context.snapshot
        section("Repository context", [
            f"{_escape(snapshot.repository, 500)}; {_escape(snapshot.status)}; "
            f"commit {_escape(snapshot.commit or 'unavailable', 100)}; profile linkage unverified",
            *[f"{_escape(item.id, 100)} — {_escape(item.path, 500)}:{item.start_line}–{item.end_line}"
              for item in report.source_context.evidence],
        ])
    references = []
    sources = (report.documentation or {}).get("sources")
    for source in (sources if isinstance(sources, list) else [])[:12]:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        label = _escape(source.get("title") or source.get("file") or "Documentation", 200)
        if isinstance(url, str) and len(url) <= 2000 and _safe_url(url):
            references.append(f"[{label}](<{url}>)")
        else:
            references.append(label + " (local documentation)")
    section("Documentation sources", references)
    if shortened:
        blocks.append("*Chat display shortened; full output remains in the run details.*")
    blocks.append(footer)
    return ChatReport(content="\n\n".join(blocks))
