"""Bounded, read-only Git object access to backend-registered local repositories."""

import hashlib
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from ..profiler_analysis_agent.source_contracts import SourceEvidence, SourceLocation
from ..runtime import remaining_timeout


class SourceUnavailable(ValueError):
    pass


class GitSourceGateway:
    def __init__(self, repositories: dict[str, str] | None = None, *, timeout: float = 5):
        self.repositories = {
            key: Path(value).resolve() for key, value in (repositories or {}).items()
        }
        self.git = shutil.which("git")
        self.timeout = timeout

    def _run(self, repository, args, *, limit=262_144):
        root = self.repositories.get(repository)
        if root is None or not root.is_dir() or self.git is None:
            raise SourceUnavailable("Repository is not registered or Git is unavailable")
        timeout = remaining_timeout(self.timeout)
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_PAGER="cat",
        )
        argv = [
            self.git,
            "--no-pager",
            "--no-replace-objects",
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            "-C",
            str(root),
            *args,
        ]
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
            shell=False,
        )
        chunks = queue.Queue(maxsize=4)
        stop = threading.Event()

        def read():
            try:
                while not stop.is_set():
                    data = proc.stdout.read(4096)
                    while not stop.is_set():
                        try:
                            chunks.put(data, timeout=0.05)
                            break
                        except queue.Full:
                            continue
                    if not data:
                        break
            except OSError:
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        end = time.monotonic() + timeout
        output = bytearray()
        try:
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise SourceUnavailable("Git source time budget exhausted")
                try:
                    data = chunks.get(timeout=min(left, 0.1))
                except queue.Empty:
                    continue
                if not data:
                    break
                if len(output) + len(data) > limit:
                    raise SourceUnavailable("Git output exceeds the configured limit")
                output.extend(data)
            if proc.wait(timeout=max(0.001, end - time.monotonic())) != 0:
                raise SourceUnavailable("Git object or revision is unavailable")
            return bytes(output)
        except subprocess.TimeoutExpired as exc:
            raise SourceUnavailable("Git source time budget exhausted") from exc
        finally:
            stop.set()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join(timeout=1)
            proc.stdout.close()

    @staticmethod
    def _oid(value):
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise SourceUnavailable("Invalid Git object identity")
        return value

    def resolve_revision(self, repository, ref):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", ref) or ".." in ref:
            raise SourceUnavailable("Unsupported revision selector")
        # Bare branch names always mean local branches, not arbitrary revision expressions.
        selector = (
            ref
            if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref) or ref.startswith("refs/")
            else "refs/heads/" + ref
        )
        commit = (
            self._run(
                repository,
                ["rev-parse", "--verify", "--end-of-options", selector + "^{commit}"],
                limit=256,
            )
            .decode()
            .strip()
        )
        self._oid(commit)
        tree = (
            self._run(
                repository,
                ["rev-parse", "--verify", "--end-of-options", commit + "^{tree}"],
                limit=256,
            )
            .decode()
            .strip()
        )
        return commit, self._oid(tree)

    @staticmethod
    def _path(path):
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or any(part in {"..", ".git"} for part in pure.parts)
            or str(pure) in {"", "."}
        ):
            raise SourceUnavailable("Select a repository-relative regular file path")
        return str(pure)

    def _blob(self, repository, commit, path):
        self._oid(commit)
        path = self._path(path)
        entry = self._run(repository, ["ls-tree", "-z", commit, "--", path], limit=4096)
        try:
            head, actual = entry.rstrip(b"\x00").split(b"\t", 1)
            mode, kind, oid = head.decode("ascii").split()
            if actual.decode("utf-8") != path or mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError
            oid = self._oid(oid)
            size = int(self._run(repository, ["cat-file", "-s", oid], limit=64))
            if size > 262_144:
                raise SourceUnavailable("Source blob exceeds 256 KiB")
            raw = self._run(repository, ["cat-file", "blob", oid], limit=262_144)
            if b"\x00" in raw or raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise SourceUnavailable("Binary and LFS pointer blobs are unsupported")
            return oid, raw.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, SourceUnavailable):
                raise
            raise SourceUnavailable("Regular UTF-8 source file is unavailable") from exc

    def read_source(self, repository, commit, location: SourceLocation, *, candidate=False):
        blob, content = self._blob(repository, commit, location.path)
        lines = content.splitlines(keepends=True)
        chosen = lines[location.start_line - 1 : location.end_line]
        if not chosen:
            raise SourceUnavailable("Requested source lines are unavailable")
        kept = []
        size = 0
        for line in chosen:
            length = len(line.encode("utf-8"))
            if size + length > 8192:
                break
            kept.append(line)
            size += length
        if not kept:
            raise SourceUnavailable("Source line exceeds the excerpt limit")
        text = "".join(kept)
        identity = f"{repository}:{commit}:{blob}:{location.path}:{location.start_line}:" + text
        return SourceEvidence(
            id="src_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            repository=repository,
            commit=commit,
            blob=blob,
            path=location.path,
            start_line=location.start_line,
            end_line=location.start_line + len(kept) - 1,
            text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            resolution="text_candidate" if candidate else "provided_location",
        )

    def find_source(self, repository, commit, query):
        self._oid(commit)
        if not query or len(query.encode("utf-8")) > 256 or any(c in query for c in "\x00\n\r"):
            raise SourceUnavailable("Provide a bounded, single-line symbol query")
        # Inspect at most 1000 regular paths / 2 MiB tree listing / 8 MiB source in total.
        listing = self._run(repository, ["ls-tree", "-r", "-z", commit], limit=2_097_152)
        entries = listing.split(b"\x00")
        if len(entries) > 1001:
            raise SourceUnavailable("Repository search scope exceeds 1000 paths; specify a file")
        hits, scanned = [], 0
        for entry in entries:
            if not entry or not entry.startswith((b"100644 blob ", b"100755 blob ")):
                continue
            try:
                path = entry.split(b"\t", 1)[1].decode("utf-8")
                _, text = self._blob(repository, commit, path)
            except SourceUnavailable:
                continue
            scanned += len(text.encode("utf-8"))
            if scanned > 8_388_608:
                raise SourceUnavailable("Source search byte budget exceeded; specify a file")
            for line, content in enumerate(text.splitlines(), 1):
                if query in content:
                    hits.append(
                        SourceLocation(path=path, start_line=max(1, line - 5), end_line=line + 10)
                    )
                    if len(hits) >= 20:
                        return hits
        return hits
