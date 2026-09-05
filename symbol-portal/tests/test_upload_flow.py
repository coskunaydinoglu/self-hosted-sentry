import zipfile

import pytest

from app.difs import chunk_file, collect_candidates
from app.uploader import upload_candidates
from tests.test_difs import MACHO, MAPPING, make_dsym_zip


@pytest.mark.asyncio
async def test_native_upload_goes_through_chunk_protocol(tmp_path, fake, client):
    archive = make_dsym_zip(tmp_path / "MyApp.dSYM.zip")
    candidates = collect_candidates("MyApp.dSYM.zip", archive, tmp_path)

    results = await upload_candidates(client, "ios-app", candidates, tmp_path)

    assert [r.status for r in results] == ["uploaded", "uploaded"]
    assert {r.object_name for r in results} == {"MyApp", "Pods"}
    assert all(r.debug_id for r in results)
    # every distinct 4-byte chunk of both binaries reached Sentry (identical chunks are deduplicated)
    expected = {c.sha1 for cand in candidates for c in chunk_file(cand.path, 4).chunks}
    assert set(fake.chunks) == expected
    # the chunk-upload URL was rewritten from the public host to the internal one
    posts = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/chunk-upload/")]
    assert posts and all(r.url.host == "web" for r in posts)
    # batching respected chunksPerRequest = 2
    assert all(r.content.count(b'filename="') <= 2 for r in posts)


@pytest.mark.asyncio
async def test_reupload_reports_exists(tmp_path, fake, client):
    binary = tmp_path / "MyApp"
    binary.write_bytes(MACHO)
    candidates = collect_candidates("MyApp", binary, tmp_path)
    first = await upload_candidates(client, "ios-app", candidates, tmp_path)
    assert first[0].status == "uploaded"
    fake.pending_polls = 0
    second = await upload_candidates(client, "ios-app", candidates, tmp_path)
    assert second[0].status == "exists"
    assert second[0].debug_id == first[0].debug_id


@pytest.mark.asyncio
async def test_proguard_upload_uses_legacy_zip_with_uuid(tmp_path, fake, client):
    mapping = tmp_path / "mapping.txt"
    mapping.write_bytes(MAPPING)
    candidates = collect_candidates("mapping.txt", mapping, tmp_path)
    uuid = "8a5f3c2e-1b4d-4e6f-9a0b-1c2d3e4f5a6b"

    (result,) = await upload_candidates(client, "android-app", candidates, tmp_path, proguard_uuid=uuid)

    assert result.status == "uploaded"
    assert result.debug_id == uuid
    assert fake.archives == [[f"proguard/{uuid}.txt"]]


@pytest.mark.asyncio
async def test_proguard_upload_without_uuid_generates_one(tmp_path, fake, client):
    mapping = tmp_path / "mapping.txt"
    mapping.write_bytes(MAPPING)
    candidates = collect_candidates("mapping.txt", mapping, tmp_path)
    (result,) = await upload_candidates(client, "android-app", candidates, tmp_path)
    assert result.status == "uploaded"
    assert "io.sentry.proguard-uuid" in result.message
    assert fake.archives[0][0] == f"proguard/{result.debug_id}.txt"


@pytest.mark.asyncio
async def test_invalid_proguard_uuid_is_reported(tmp_path, fake, client):
    mapping = tmp_path / "mapping.txt"
    mapping.write_bytes(MAPPING)
    candidates = collect_candidates("mapping.txt", mapping, tmp_path)
    (result,) = await upload_candidates(client, "android-app", candidates, tmp_path, proguard_uuid="nope")
    assert result.status == "error"
    assert fake.archives == []


@pytest.mark.asyncio
async def test_oversized_file_is_rejected_locally(tmp_path, fake, client):
    fake.chunk_options["maxFileSize"] = 10
    binary = tmp_path / "big"
    binary.write_bytes(MACHO)
    (result,) = await upload_candidates(client, "ios-app", collect_candidates("big", binary, tmp_path), tmp_path)
    assert result.status == "error"
    assert fake.chunks == {}


@pytest.mark.asyncio
async def test_sentry_error_is_captured_per_file(tmp_path, fake, client):
    fake.chunk_options = {"url": "https://sentry.example.com/api/0/organizations/sentry/chunk-upload/", "chunkSize": 4}

    def failing(request):
        if request.url.path.endswith("/assemble/"):
            import httpx

            return httpx.Response(403, json={"detail": "You do not have permission to perform this action."})
        return fake.handler(request)

    import httpx

    from app.sentry_api import SentryClient

    client = SentryClient(
        "http://web:9000", "sentry", "test-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(failing), headers={"Authorization": "Bearer test-token"}),
    )
    binary = tmp_path / "MyApp"
    binary.write_bytes(MACHO)
    (result,) = await upload_candidates(client, "ios-app", collect_candidates("MyApp", binary, tmp_path), tmp_path)
    assert result.status == "error"
    assert "403" in result.message and "permission" in result.message
