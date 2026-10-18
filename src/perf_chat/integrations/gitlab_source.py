"""Read-only GitLab API source access; credentials stay in headers on one HTTPS origin."""

import asyncio
import base64
import hashlib
import json
from urllib.parse import quote, urlsplit

import httpx

from ..profiler_analysis_agent.source_contracts import SourceLocation
from ..runtime import remaining_timeout
from .git_source import GitSourceGateway, SourceUnavailable


class UnsupportedSource(SourceUnavailable):
    """A file whose content cannot be used as source evidence."""


def repository_url(value):
    url = urlsplit(value)
    if (
        "\\" in value
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise SourceUnavailable("Use an HTTPS GitLab URL without embedded credentials")
    project = url.path.strip("/")
    if project.endswith(".git"):
        project = project[:-4]
    if "/" not in project or any(part in {"", ".", ".."} for part in project.split("/")):
        raise SourceUnavailable("Use a GitLab group/project repository URL")
    return f"https://{url.netloc}/api/v4/projects/{quote(project, safe='')}"


def object_hash(kind, data, oid):
    algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
    return algorithm(kind.encode() + b" " + str(len(data)).encode() + b"\x00" + data).hexdigest()


class GitLabSourceGateway:
    def __init__(self, repository, token="", *, transport=None, timeout=5):
        self.repository = repository
        self.base = repository_url(repository)
        self._token = token
        self.transport = transport
        self.timeout = timeout

    def _json(self, path, params=None, *, metadata=False):
        timeout = remaining_timeout(self.timeout)

        async def request():
            async with httpx.AsyncClient(
                headers={"PRIVATE-TOKEN": self._token} if self._token else {},
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "HEAD" if metadata else "GET", self.base + path, params=params
                ) as response:
                    if response.status_code != 200:
                        raise SourceUnavailable(
                            "GitLab source request failed; check repository access and token"
                        )
                    if metadata:
                        return dict(response.headers)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 1_000_000:
                            raise SourceUnavailable("GitLab response exceeds the source limit")
                        data.extend(chunk)
                    return json.loads(data)

        async def bounded():
            return await asyncio.wait_for(request(), timeout)

        return asyncio.run(bounded())

    def _check(self, repository):
        if repository != self.repository:
            raise SourceUnavailable("Token is bound to a different repository")

    def _tree(self, commit, path="", recursive=False):
        items = []
        for page in range(1, 12):
            data = self._json(
                "/repository/tree",
                {
                    "ref": commit,
                    "path": path,
                    "recursive": "true" if recursive else "false",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(data, list) or len(items) + len(data) > 1000:
                raise SourceUnavailable(
                    "GitLab tree exceeds the search limit; select a smaller repository"
                )
            items.extend(data)
            if len(data) < 100:
                return items
        raise SourceUnavailable("GitLab tree pagination limit exceeded")

    def resolve_revision(self, repository, ref):
        self._check(repository)
        if not ref or ref.startswith("-") or len(ref) > 256 or any(c in ref for c in "\r\n\x00"):
            raise SourceUnavailable("Invalid revision selector")
        commit = GitSourceGateway._oid(
            self._json("/repository/commits/" + quote(ref, safe=""))["id"]
        )
        entries = self._tree(commit)
        tree = bytearray()
        for entry in sorted(
            entries,
            key=lambda item: item["name"].encode() + (b"/" if item["type"] == "tree" else b""),
        ):
            mode, name, oid = (
                entry["mode"].lstrip("0"),
                entry["name"],
                GitSourceGateway._oid(entry["id"]),
            )
            if "/" in name or "\x00" in name or len(oid) != len(commit):
                raise SourceUnavailable("Unsupported GitLab tree identity")
            tree.extend(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(oid)
            )
        return commit, object_hash("tree", bytes(tree), commit)

    def _blob(self, repository, commit, path):
        self._check(repository)
        GitSourceGateway._oid(commit)
        path = GitSourceGateway._path(path)
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        entries = self._tree(commit, parent)
        match = next((item for item in entries if item["path"] == path), None)
        if not match or match["mode"] not in {"100644", "100755"} or match["type"] != "blob":
            raise SourceUnavailable("GitLab regular source file is unavailable")
        oid = GitSourceGateway._oid(match["id"])
        metadata = self._json(
            "/repository/files/" + quote(path, safe=""), {"ref": commit}, metadata=True
        )
        if metadata.get("x-gitlab-blob-id") != oid:
            raise SourceUnavailable("GitLab source file changed identity")
        if int(metadata.get("x-gitlab-size", "262145")) > 262144:
            raise UnsupportedSource("GitLab source file exceeds 256 KiB")
        info = self._json("/repository/blobs/" + oid)
        if info.get("size", 262145) > 262144 or info.get("encoding") != "base64":
            raise UnsupportedSource("GitLab blob exceeds 256 KiB or has unsupported encoding")
        raw = base64.b64decode("".join(info["content"].split()), validate=True)
        if len(raw) > 262144 or object_hash("blob", raw, oid) != oid:
            raise SourceUnavailable("GitLab blob size or identity mismatch")
        if b"\x00" in raw or raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise UnsupportedSource("Binary and LFS pointer blobs are unsupported")
        try:
            return oid, raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedSource("Source must be UTF-8") from exc

    read_source = GitSourceGateway.read_source

    def find_source(self, repository, commit, query):
        self._check(repository)
        if not query or len(query.encode()) > 256 or any(c in query for c in "\r\n\x00"):
            raise SourceUnavailable("Provide a bounded single-line source query")
        hits, scanned = [], 0
        for entry in self._tree(commit, recursive=True):
            if entry["mode"] not in {"100644", "100755"}:
                continue
            try:
                _, text = self._blob(repository, commit, entry["path"])
            except UnsupportedSource:
                continue
            scanned += len(text.encode())
            if scanned > 8_388_608:
                raise SourceUnavailable("Source search byte limit exceeded")
            for line, content in enumerate(text.splitlines(), 1):
                if query in content:
                    hits.append(
                        SourceLocation(
                            path=entry["path"], start_line=max(1, line - 5), end_line=line + 10
                        )
                    )
                    if len(hits) >= 20:
                        return hits
        return hits


class RepositorySourceGateway:
    def __init__(self, local, *, repository="", token=""):
        self.local = local
        self.repository = repository
        self._token = token
        self.remote = None

    def __getattr__(self, name):
        if self.repository.startswith("https://") and self.remote is None:
            self.remote = GitLabSourceGateway(self.repository, self._token)
        return getattr(self.remote or self.local, name)
