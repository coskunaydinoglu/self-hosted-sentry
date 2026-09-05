"""Turn whatever a developer drops on the page into individual debug information files.

Developers hand us dSYM zips straight out of Xcode (or App Store Connect), raw
DWARF/ELF binaries, Breakpad .sym files or ProGuard/R8 mapping.txt files. Sentry
wants each debug file on its own, so we unpack archives here, drop the noise
(Info.plist, __MACOSX, Relative/ swiftmodule maps) and classify what is left.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

JUNK_BASENAMES = {".DS_Store", "Info.plist", "Thumbs.db"}
JUNK_SUFFIXES = {".plist", ".yml", ".yaml", ".json", ".xcconfig", ".txt.gz"}
JUNK_PATH_PARTS = {"__MACOSX", "Relative"}

PROGUARD_NAMESPACE = uuid.UUID("2a34d3f9-7c48-4a5b-9d1e-4f6c7b8a9d0e")


@dataclass
class Candidate:
    """A single file we intend to send to Sentry."""

    name: str  # name reported to Sentry (basename, like sentry-cli does)
    path: Path
    source: str  # archive entry or original upload name, for the UI
    kind: str  # "native" or "proguard"
    size: int = 0

    @classmethod
    def from_path(cls, path: Path, source: str) -> "Candidate":
        kind = "proguard" if is_proguard_mapping(path) else "native"
        return cls(name=path.name, path=path, source=source, kind=kind, size=path.stat().st_size)


@dataclass
class Chunk:
    sha1: str
    offset: int
    length: int


@dataclass
class ChunkedFile:
    sha1: str
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def chunk_hashes(self) -> list[str]:
        return [c.sha1 for c in self.chunks]


class UnsafeArchive(ValueError):
    pass


def is_zip(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def is_junk(relpath: str) -> bool:
    parts = relpath.split("/")
    if any(p in JUNK_PATH_PARTS for p in parts):
        return True
    base = parts[-1]
    if base in JUNK_BASENAMES or base.startswith("._"):
        return True
    return any(base.endswith(suffix) for suffix in JUNK_SUFFIXES)


def is_proguard_mapping(path: Path) -> bool:
    """ProGuard/R8 mapping files are UTF-8 text with `original -> obfuscated` lines."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return False
    if not head or b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return " -> " in text


def extract_archive(archive: Path, dest: Path) -> list[Path]:
    """Safely extract a zip (no zip-slip, no symlinks) and return extracted regular files."""
    extracted: list[Path] = []
    dest = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            relpath = posixpath.normpath(info.filename.replace("\\", "/"))
            if relpath.startswith("../") or relpath.startswith("/") or relpath == "..":
                raise UnsafeArchive(f"archive entry escapes destination: {info.filename}")
            # Skip symlinks (mode in the high 16 bits of external_attr).
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            if is_junk(relpath):
                continue
            target = (dest / relpath).resolve()
            if dest not in target.parents:
                raise UnsafeArchive(f"archive entry escapes destination: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
            extracted.append(target)
    return extracted


def collect_candidates(upload_name: str, path: Path, workdir: Path) -> list[Candidate]:
    """Expand one uploaded file into debug-file candidates."""
    if not is_zip(path):
        if path.stat().st_size == 0 or is_junk(upload_name):
            return []
        return [Candidate.from_path(path, source=upload_name)]

    extract_dir = workdir / f"extract-{uuid.uuid4().hex}"
    extract_dir.mkdir(parents=True)
    candidates: list[Candidate] = []
    for extracted in extract_archive(path, extract_dir):
        if extracted.stat().st_size == 0:
            continue
        rel = extracted.relative_to(extract_dir).as_posix()
        # Nested zips (e.g. an archive of several dSYM zips) are expanded one level.
        if is_zip(extracted):
            candidates.extend(collect_candidates(f"{upload_name}!{rel}", extracted, workdir))
            continue
        candidates.append(Candidate.from_path(extracted, source=f"{upload_name}!{rel}"))
    return candidates


def chunk_file(path: Path, chunk_size: int) -> ChunkedFile:
    total = hashlib.sha1()
    chunks: list[Chunk] = []
    offset = 0
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk_size)
            if not data:
                break
            total.update(data)
            chunks.append(Chunk(sha1=hashlib.sha1(data).hexdigest(), offset=offset, length=len(data)))
            offset += len(data)
    return ChunkedFile(sha1=total.hexdigest(), chunks=chunks)


def read_chunk(path: Path, chunk: Chunk) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(chunk.offset)
        return fh.read(chunk.length)


def derive_proguard_uuid(path: Path) -> str:
    """Deterministic UUID for a mapping file when the developer does not supply one."""
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return str(uuid.uuid5(PROGUARD_NAMESPACE, digest.hexdigest()))


def package_proguard(path: Path, mapping_uuid: str, workdir: Path) -> Path:
    """Wrap a mapping file the way sentry-cli does: a zip containing proguard/<uuid>.txt."""
    mapping_uuid = str(uuid.UUID(mapping_uuid))  # validates the format
    out = workdir / f"proguard-{mapping_uuid}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=f"proguard/{mapping_uuid}.txt")
    return out


def safe_filename(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")) or "upload"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base)[:200]
