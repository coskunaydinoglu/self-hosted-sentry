"""Upload flows: chunked upload for native debug files, legacy zip upload for ProGuard."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .difs import Candidate, chunk_file, derive_proguard_uuid, package_proguard, read_chunk
from .sentry_api import SentryClient, SentryError

FINAL_STATES = {"ok", "error", "not_found"}


@dataclass
class UploadResult:
    source: str
    name: str
    kind: str
    size: int
    status: str  # "uploaded", "exists", "error", "skipped"
    message: str = ""
    debug_id: str | None = None
    object_name: str | None = None
    cpu_name: str | None = None
    details: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _dif_summary(dif: dict | None) -> dict:
    if not dif:
        return {}
    return {
        "debug_id": dif.get("debugId") or dif.get("uuid"),
        "object_name": dif.get("objectName"),
        "cpu_name": dif.get("cpuName"),
    }


async def upload_native(
    client: SentryClient,
    project: str,
    candidate: Candidate,
    options: dict,
    poll_interval: float = 1.0,
    max_polls: int = 120,
) -> UploadResult:
    chunk_size = int(options.get("chunkSize") or 8 * 1024 * 1024)
    max_file_size = int(options.get("maxFileSize") or 0)
    if max_file_size and candidate.size > max_file_size:
        return UploadResult(
            candidate.source, candidate.name, candidate.kind, candidate.size, "error",
            f"Dosya Sentry'nin izin verdiği boyutu aşıyor ({max_file_size} bayt)",
        )

    chunked = chunk_file(candidate.path, chunk_size)
    payload = {chunked.sha1: {"name": candidate.name, "chunks": chunked.chunk_hashes}}

    state = (await client.assemble_difs(project, payload)).get(chunked.sha1, {})
    if state.get("state") == "ok":
        return UploadResult(
            candidate.source, candidate.name, candidate.kind, candidate.size, "exists",
            "Bu debug dosyası zaten yüklüydü", **_dif_summary(state.get("dif")),
        )

    missing = set(state.get("missingChunks") or chunked.chunk_hashes)
    to_send = [c for c in chunked.chunks if c.sha1 in missing]
    per_request = max(1, int(options.get("chunksPerRequest") or 64))
    max_request = int(options.get("maxRequestSize") or 32 * 1024 * 1024)

    batch: list[tuple[str, bytes]] = []
    batch_size = 0
    for chunk in to_send:
        if batch and (len(batch) >= per_request or batch_size + chunk.length > max_request):
            await client.upload_chunks(options["url"], batch)
            batch, batch_size = [], 0
        batch.append((chunk.sha1, read_chunk(candidate.path, chunk)))
        batch_size += chunk.length
    if batch:
        await client.upload_chunks(options["url"], batch)

    for _ in range(max_polls):
        state = (await client.assemble_difs(project, payload)).get(chunked.sha1, {})
        if state.get("state") in FINAL_STATES:
            break
        await asyncio.sleep(poll_interval)

    status = state.get("state")
    if status == "ok":
        return UploadResult(
            candidate.source, candidate.name, candidate.kind, candidate.size, "uploaded",
            "Yüklendi", **_dif_summary(state.get("dif")),
        )
    if status == "not_found":
        return UploadResult(
            candidate.source, candidate.name, candidate.kind, candidate.size, "error",
            "Sentry yüklenen parçaları bulamadı; tekrar deneyin",
        )
    if status == "error":
        return UploadResult(
            candidate.source, candidate.name, candidate.kind, candidate.size, "error",
            state.get("detail") or "Sentry dosyayı işleyemedi (desteklenmeyen format olabilir)",
        )
    return UploadResult(
        candidate.source, candidate.name, candidate.kind, candidate.size, "error",
        "Sentry dosyayı işlemeyi zamanında bitiremedi; Debug Files sayfasını kontrol edin",
    )


async def upload_proguard(
    client: SentryClient, project: str, candidate: Candidate, mapping_uuid: str | None, workdir: Path
) -> UploadResult:
    generated = not mapping_uuid
    mapping_uuid = mapping_uuid or derive_proguard_uuid(candidate.path)
    try:
        archive = package_proguard(candidate.path, mapping_uuid, workdir)
    except ValueError:
        return UploadResult(
            candidate.source, candidate.name, candidate.kind, candidate.size, "error",
            f"Geçersiz ProGuard UUID: {mapping_uuid}",
        )
    difs = await client.upload_dif_archive(project, archive)
    note = "Yüklendi"
    if generated:
        note = (
            "Yüklendi. UUID portal tarafından üretildi; uygulamanın bu UUID'yi "
            "io.sentry.proguard-uuid olarak bildirmesi gerekir (Sentry Gradle Plugin bunu otomatik yapar)."
        )
    summary = _dif_summary(difs[0] if difs else None)
    return UploadResult(
        candidate.source, candidate.name, candidate.kind, candidate.size, "uploaded", note,
        debug_id=summary.get("debug_id") or mapping_uuid,
        object_name=summary.get("object_name") or "proguard-mapping",
        cpu_name=summary.get("cpu_name") or "any",
        details=difs,
    )


async def upload_candidates(
    client: SentryClient,
    project: str,
    candidates: list[Candidate],
    workdir: Path,
    proguard_uuid: str | None = None,
) -> list[UploadResult]:
    results: list[UploadResult] = []
    options: dict | None = None
    for candidate in candidates:
        try:
            if candidate.kind == "proguard":
                results.append(await upload_proguard(client, project, candidate, proguard_uuid, workdir))
                continue
            if options is None:
                options = await client.chunk_upload_options()
            results.append(await upload_native(client, project, candidate, options))
        except SentryError as exc:
            results.append(
                UploadResult(candidate.source, candidate.name, candidate.kind, candidate.size, "error", str(exc))
            )
    return results
