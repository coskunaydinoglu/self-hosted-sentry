import zipfile
from pathlib import Path

import pytest

from app.difs import (
    UnsafeArchive,
    chunk_file,
    collect_candidates,
    derive_proguard_uuid,
    extract_archive,
    is_proguard_mapping,
    package_proguard,
    safe_filename,
)

MAPPING = b"# compiler: R8\ncom.example.Foo -> a.b:\n    int field -> a\n"
MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 60


def make_dsym_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("MyApp.app.dSYM/Contents/Info.plist", "<plist/>")
        zf.writestr("MyApp.app.dSYM/Contents/Resources/DWARF/MyApp", MACHO)
        zf.writestr("MyApp.app.dSYM/Contents/Resources/Relative/MyApp-arm64.yml", "---")
        zf.writestr("__MACOSX/._MyApp.app.dSYM", "junk")
        zf.writestr("Pods.framework.dSYM/Contents/Resources/DWARF/Pods", MACHO + b"\x01")
    return path


def test_dsym_zip_yields_only_dwarf_files(tmp_path):
    archive = make_dsym_zip(tmp_path / "MyApp.dSYM.zip")
    candidates = collect_candidates("MyApp.dSYM.zip", archive, tmp_path)
    assert sorted(c.name for c in candidates) == ["MyApp", "Pods"]
    assert all(c.kind == "native" for c in candidates)
    assert candidates[0].source.startswith("MyApp.dSYM.zip!")


def test_nested_zip_is_expanded(tmp_path):
    inner = make_dsym_zip(tmp_path / "inner.zip")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, "dsyms/inner.zip")
    names = sorted(c.name for c in collect_candidates("outer.zip", outer, tmp_path))
    assert names == ["MyApp", "Pods"]


def test_raw_mapping_file_is_detected(tmp_path):
    mapping = tmp_path / "mapping.txt"
    mapping.write_bytes(MAPPING)
    assert is_proguard_mapping(mapping)
    (candidate,) = collect_candidates("mapping.txt", mapping, tmp_path)
    assert candidate.kind == "proguard"


def test_binary_is_not_mapping(tmp_path):
    binary = tmp_path / "libfoo.so"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 20)
    assert not is_proguard_mapping(binary)
    (candidate,) = collect_candidates("libfoo.so", binary, tmp_path)
    assert candidate.kind == "native"


def test_zip_slip_is_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../etc/passwd", "x")
    with pytest.raises(UnsafeArchive):
        extract_archive(evil, tmp_path / "out")


def test_chunking_matches_sha1(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(b"abcdefghij")
    chunked = chunk_file(f, 4)
    assert [c.length for c in chunked.chunks] == [4, 4, 2]
    assert chunked.sha1 == __import__("hashlib").sha1(b"abcdefghij").hexdigest()
    assert chunked.chunks[1].offset == 4


def test_proguard_packaging(tmp_path):
    mapping = tmp_path / "mapping.txt"
    mapping.write_bytes(MAPPING)
    generated = derive_proguard_uuid(mapping)
    assert generated == derive_proguard_uuid(mapping)  # deterministic
    archive = package_proguard(mapping, generated, tmp_path)
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == [f"proguard/{generated}.txt"]
    with pytest.raises(ValueError):
        package_proguard(mapping, "not-a-uuid", tmp_path)


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\x\\My App.dSYM.zip") == "My_App.dSYM.zip"
