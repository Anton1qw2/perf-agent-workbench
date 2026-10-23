"""Bounded, read-only SKILLS.md snapshots. Knowledge never authorizes tools."""

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from ..contracts import StrictModel
from ..runtime import check_deadline

MAX_DOCUMENTS = 128
MAX_DIRECTORIES = 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 8
MAX_DOCUMENT_BYTES = 65_536
MAX_SNAPSHOT_BYTES = 1_000_000


class KnowledgeDocument(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=MAX_DOCUMENT_BYTES)


class KnowledgeSnapshot(StrictModel):
    status: Literal["disabled", "ready", "empty", "unavailable"] = "disabled"
    documents: list[KnowledgeDocument] = Field(default_factory=list, max_length=MAX_DOCUMENTS)
    limitations: list[str] = Field(default_factory=list)


class KnowledgeReference(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class KnowledgeSelection(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    paths: list[str] = Field(default_factory=list, max_length=4)


class KnowledgeGateway(Protocol):
    def snapshot(self) -> KnowledgeSnapshot: ...


def _linked(info):
    # Windows junctions and other reparse points are excluded as well as symlinks.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    )


class DirectoryKnowledgeGateway:
    def __init__(self, root: str = ""):
        self.root = Path(root).absolute() if root else None

    def snapshot(self):
        if self.root is None:
            return KnowledgeSnapshot()
        try:
            # Reject linked ancestors too: selecting a junction must not escape the root.
            for component in [*reversed(self.root.parents), self.root]:
                if _linked(component.lstat()):
                    raise ValueError("Linked knowledge roots are unsupported")
            pending = [(self.root, 0)]
            documents, total, directories, entry_count = [], 0, 0, 0
            while pending:
                check_deadline()
                directory, depth = pending.pop()
                directories += 1
                if directories > MAX_DIRECTORIES:
                    raise ValueError("Knowledge directory limit exceeded")
                with os.scandir(directory) as entries:
                    children = []
                    for entry in entries:
                        check_deadline()
                        entry_count += 1
                        if entry_count > MAX_ENTRIES:
                            raise ValueError("Knowledge entry limit exceeded")
                        info = entry.stat(follow_symlinks=False)
                        if _linked(info):
                            raise ValueError("Linked knowledge entries are unsupported")
                        if stat.S_ISDIR(info.st_mode):
                            if depth >= MAX_DEPTH:
                                raise ValueError("Knowledge depth limit exceeded")
                            children.append((Path(entry.path), depth + 1))
                            if directories + len(pending) + len(children) > MAX_DIRECTORIES:
                                raise ValueError("Knowledge directory limit exceeded")
                        elif entry.name == "SKILLS.md":
                            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES:
                                raise ValueError("Invalid knowledge document")
                            path = Path(entry.path)
                            # DirEntry.stat() reports zero file IDs on Windows; lstat does not.
                            identity = path.lstat()
                            if _linked(identity) or not stat.S_ISREG(identity.st_mode):
                                raise ValueError("Knowledge file changed during snapshot")
                            with path.open("rb") as handle:
                                opened = os.fstat(handle.fileno())
                                if (not stat.S_ISREG(opened.st_mode)
                                        or (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino)):
                                    raise ValueError("Knowledge file changed during snapshot")
                                data = handle.read(MAX_DOCUMENT_BYTES + 1)
                            total += len(data)
                            if (len(data) > MAX_DOCUMENT_BYTES or total > MAX_SNAPSHOT_BYTES
                                    or len(documents) >= MAX_DOCUMENTS):
                                raise ValueError("Knowledge snapshot limit exceeded")
                            documents.append(KnowledgeDocument(
                                path=path.relative_to(self.root).as_posix(),
                                sha256=hashlib.sha256(data).hexdigest(),
                                content=data.decode("utf-8"),
                            ))
                    pending.extend(sorted(children, reverse=True))
            check_deadline()
            return KnowledgeSnapshot(
                status="ready" if documents else "empty",
                documents=sorted(documents, key=lambda item: item.path),
                limitations=[] if documents else ["Knowledge tree contains no SKILLS.md documents."],
            )
        except TimeoutError:
            raise
        except (OSError, UnicodeError, ValueError):
            # Never expose absolute paths or OS exception details to the model or chat.
            return KnowledgeSnapshot(status="unavailable", limitations=[
                "Knowledge snapshot unavailable: use a bounded tree of regular UTF-8 SKILLS.md files "
                "without links or junctions. No partial snapshot was accepted.",
            ])


def knowledge_catalog(snapshot: KnowledgeSnapshot):
    """Small descriptions for semantic branch selection; no executable skill metadata."""
    catalog = []
    for document in snapshot.documents:
        # Front matter is reference text, not a YAML object or configuration to execute.
        description = " ".join(document.content.split())[:256]
        catalog.append({"path": document.path, "description": description})
    return catalog


def select_knowledge(snapshot: KnowledgeSnapshot, paths: list[str]):
    available = {item.path: item for item in snapshot.documents}
    selected = {"SKILLS.md"} if "SKILLS.md" in available else set()
    for path in paths:
        if path not in available:
            raise ValueError("Knowledge selection must reference a snapshot catalog path")
        node = PurePosixPath(path)
        selected.add(path)
        for parent in node.parents:
            candidate = (parent / "SKILLS.md").as_posix()
            if candidate in available:
                selected.add(candidate)
    return [available[path] for path in sorted(selected, key=lambda p: (p.count("/"), p))]
