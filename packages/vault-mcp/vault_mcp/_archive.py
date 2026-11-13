"""Fetch a zip or tar archive from an https URL and list its files, for import_archive.

Everything is bounded: the download size and time, the number of files, and
each file's size (checked while reading, so a lying zip header can't inflate
it). Only public hosts are fetched, so a URL can't reach the instance metadata
service or anything else on the box's network. Directories, links, hidden
entries (``.obsidian/``, ``__MACOSX/``, ``.DS_Store``) and paths with ``..`` are
never returned.
"""

import io
import ipaddress
import socket
import tarfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

MAX_DOWNLOAD = 100 * 2**20
MAX_TOTAL = 300 * 2**20  # uncompressed
MAX_FILES = 2000
# Obsidian Sync Standard won't sync a bigger file.
MAX_FILE = 5 * 2**20
# Keeps the tool inside the front end's 25 s call timeout.
FETCH_SECONDS = 15.0


class ArchiveError(ValueError):
    """An archive that can't be fetched or read."""


@dataclass
class Member:
    path: str  # POSIX, relative, safe
    size: int
    read: Callable[[], bytes]


def _check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ArchiveError(f"only https URLs can be imported: {url[:100]!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443)
    except OSError as exc:
        raise ArchiveError(f"can't resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ArchiveError(f"{parts.hostname} is not a public host")


def fetch(url: str, timeout: float = FETCH_SECONDS) -> bytes:
    """The body at ``url``, following up to 5 redirects (each one checked)."""
    deadline = time.monotonic() + timeout
    too_slow = ArchiveError(
        f"the download didn't finish within {timeout:.0f} s (a slow server or a "
        "big archive); try again, or split the archive"
    )
    for _ in range(6):
        _check_public(url)
        left = deadline - time.monotonic()
        if left <= 0:
            raise too_slow
        try:
            with httpx.stream("GET", url, timeout=left) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    raise ArchiveError(f"download failed: HTTP {r.status_code}")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_DOWNLOAD:
                        raise ArchiveError(
                            f"archive is over {MAX_DOWNLOAD // 2**20} MiB"
                        )
                    if time.monotonic() > deadline:
                        raise too_slow
                return bytes(buf)
        except httpx.TimeoutException as exc:
            raise too_slow from exc
        except httpx.HTTPError as exc:
            raise ArchiveError(f"download failed: {exc}") from exc
    raise ArchiveError("too many redirects")


def _safe(name: str) -> tuple[str | None, str]:
    """``(path, "")`` for a usable entry name, else ``(None, reason)``."""
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        return None, "unsafe path"
    if any(p.startswith(".") or p == "__MACOSX" for p in pure.parts):
        return None, "hidden"
    return pure.as_posix(), ""


def _bounded(read: Callable[[int], bytes], size: int) -> Callable[[], bytes]:
    def go() -> bytes:
        data = read(size + 1)
        if len(data) != size:
            raise ArchiveError("an archive entry's size doesn't match its header")
        return data

    return go


def members(data: bytes) -> tuple[list[Member], list[dict[str, str]]]:
    """``(files, skipped)`` of a zip or tar (.tar, .tar.gz, .tgz, .tar.bz2, .tar.xz)."""
    files: list[Member] = []
    skipped: list[dict[str, str]] = []

    def add(name: str, size: int, read: Callable[[int], bytes]) -> None:
        path, reason = _safe(name)
        if path is None:
            if reason != "hidden":
                skipped.append({"path": name, "reason": reason})
        elif size > MAX_FILE:
            skipped.append(
                {"path": path, "reason": f"over {MAX_FILE // 2**20} MB (Sync's limit)"}
            )
        else:
            files.append(Member(path, size, _bounded(read, size)))

    if zipfile.is_zipfile(io.BytesIO(data)):
        zf = zipfile.ZipFile(io.BytesIO(data))
        for info in zf.infolist():
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                skipped.append({"path": info.filename, "reason": "symlink"})
                continue
            add(
                info.filename,
                info.file_size,
                lambda n, i=info: zf.open(i).read(n),
            )
    else:
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
            entries = tf.getmembers()
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ArchiveError("not a zip or tar archive") from exc
        for m in entries:
            if m.isdir():
                continue
            if not m.isfile():
                skipped.append({"path": m.name, "reason": "link or special file"})
                continue

            def read(n: int, m: tarfile.TarInfo = m) -> bytes:
                f = tf.extractfile(m)
                return f.read(n) if f else b""

            add(m.name, m.size, read)
    if len(files) > MAX_FILES:
        raise ArchiveError(f"archive has {len(files)} files; the limit is {MAX_FILES}")
    if sum(f.size for f in files) > MAX_TOTAL:
        raise ArchiveError(f"archive unpacks to over {MAX_TOTAL // 2**20} MiB")
    return files, skipped


def strip_common_folder(files: list[Member]) -> str | None:
    """Drop one top folder that wraps everything (GitHub and Finder archives); return it."""
    tops = {f.path.split("/", 1)[0] for f in files}
    if len(tops) != 1 or any("/" not in f.path for f in files):
        return None
    top = tops.pop()
    for f in files:
        f.path = f.path.split("/", 1)[1]
    return top
