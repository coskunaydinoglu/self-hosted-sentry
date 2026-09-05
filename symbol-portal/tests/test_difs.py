import zipfile
from pathlib import Path

import pytest

from app.difs import (
    UnsafeArchive,
    fat_slices,
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


def make_fat(slices):
    """Build a universal Mach-O container from (cputype, cpusubtype, payload) triples."""
    import struct

    header = struct.pack(">II", 0xCAFEBABE, len(slices))
    offset = 8 + 20 * len(slices)
    archs = b""
    payloads = b""
    for cputype, cpusubtype, payload in slices:
        archs += struct.pack(">IIIII", cputype, cpusubtype, offset, len(payload), 0)
        payloads += payload
        offset += len(payload)
    return header + archs + payloads


def test_fat_macho_is_split_per_architecture(tmp_path):
    arm64 = b"\xcf\xfa\xed\xfe" + b"A" * 40
    x86 = b"\xcf\xfa\xed\xfe" + b"X" * 24
    fat = tmp_path / "Example"
    fat.write_bytes(make_fat([(0x0100000C, 0, arm64), (0x01000007, 3, x86)]))

    assert [(a, s) for a, _, s in fat_slices(fat)] == [("arm64", len(arm64)), ("x86_64", len(x86))]
    candidates = collect_candidates("Example", fat, tmp_path)
    assert [c.source for c in candidates] == ["Example [arm64]", "Example [x86_64]"]
    assert all(c.name == "Example" and c.kind == "native" for c in candidates)
    assert candidates[0].path.read_bytes() == arm64 and candidates[1].path.read_bytes() == x86


def test_fat_detection_ignores_java_class_and_thin_objects(tmp_path):
    java = tmp_path / "Foo.class"
    java.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 16)
    assert fat_slices(java) is None
    thin = tmp_path / "thin"
    thin.write_bytes(MACHO)
    assert fat_slices(thin) is None
    truncated = tmp_path / "bad"
    truncated.write_bytes(make_fat([(0x0100000C, 0, b"x" * 10)])[:-5])
    assert fat_slices(truncated) is None


def test_fat_inside_dsym_zip_yields_one_candidate_per_slice(tmp_path):
    archive = tmp_path / "Example.framework.dSYM.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Example.framework.dSYM/Contents/Info.plist", "<plist/>")
        zf.writestr("Example.framework.dSYM/Contents/Resources/DWARF/Example",
                    make_fat([(0x0100000C, 2, MACHO), (0x0000000C, 9, MACHO + b"1")]))
    candidates = collect_candidates("Example.framework.dSYM.zip", archive, tmp_path)
    assert {c.source.rsplit(" ", 1)[1] for c in candidates} == {"[arm]", "[arm64e]"}
