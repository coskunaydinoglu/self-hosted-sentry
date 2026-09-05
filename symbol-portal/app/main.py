"""FastAPI application for the Symbol Portal."""

from __future__ import annotations

import base64
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import Settings, load_settings
from .difs import UnsafeArchive, collect_candidates, safe_filename
from .missing import collect_missing, parse_date, sort_entries
from .sentry_api import SentryClient, SentryError
from .uploader import upload_candidates

STATIC_DIR = Path(__file__).parent / "static"
REPORT_TTL_SECONDS = 300


def create_app(settings: Settings | None = None, client: SentryClient | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if app.state.client is not None:
            await app.state.client.aclose()

    app = FastAPI(title="Sentry Symbol Portal", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.client = client
    app.state.reports: dict[str, tuple[float, dict]] = {}
    Path(settings.tmp_dir).mkdir(parents=True, exist_ok=True)

    def get_client() -> SentryClient:
        if app.state.client is None:
            if not settings.configured:
                raise HTTPException(503, "SYMBOL_PORTAL_SENTRY_TOKEN tanımlı değil; portal yapılandırılmamış.")
            app.state.client = SentryClient(
                settings.sentry_url, settings.sentry_org, settings.sentry_token,
                rewrite_chunk_url=settings.rewrite_chunk_url,
            )
        return app.state.client

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if settings.basic_auth and request.url.path != "/api/health":
            header = request.headers.get("Authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    supplied = base64.b64decode(header[6:]).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    supplied = ""
                ok = secrets.compare_digest(supplied.encode(), settings.basic_auth.encode())
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Symbol Portal"'})
        return await call_next(request)

    @app.exception_handler(SentryError)
    async def sentry_error(_: Request, exc: SentryError):
        return JSONResponse(status_code=502, content={"detail": str(exc), "sentry": exc.detail})

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "configured": settings.configured, "org": settings.sentry_org}

    @app.get("/api/projects")
    async def projects():
        return await get_client().list_projects()

    @app.get("/api/projects/{project}/debug-files")
    async def debug_files(project: str, query: str | None = None):
        return await get_client().list_debug_files(project, query=query)

    @app.post("/api/projects/{project}/upload")
    async def upload(
        project: str,
        files: Annotated[list[UploadFile], File()],
        proguard_uuid: Annotated[str | None, Form()] = None,
    ):
        if not files:
            raise HTTPException(400, "Dosya seçilmedi")
        client = get_client()
        workdir = Path(tempfile.mkdtemp(prefix="upload-", dir=settings.tmp_dir))
        try:
            candidates = []
            skipped = []
            for upload_file in files:
                name = safe_filename(upload_file.filename or "upload")
                target = workdir / f"in-{secrets.token_hex(4)}-{name}"
                with open(target, "wb") as out:
                    shutil.copyfileobj(upload_file.file, out, length=1024 * 1024)
                try:
                    found = collect_candidates(upload_file.filename or name, target, workdir)
                except UnsafeArchive as exc:
                    skipped.append({"source": upload_file.filename, "status": "error", "message": str(exc)})
                    continue
                except Exception as exc:  # corrupt archive etc.
                    skipped.append({"source": upload_file.filename, "status": "error", "message": f"Arşiv açılamadı: {exc}"})
                    continue
                if not found:
                    skipped.append(
                        {"source": upload_file.filename, "status": "skipped", "message": "İçinde debug dosyası bulunamadı"}
                    )
                candidates.extend(found)
            results = await upload_candidates(client, project, candidates, workdir, (proguard_uuid or "").strip() or None)
            app.state.reports.pop(project, None)  # uploads change the "still missing" answer
            return {"results": [r.to_dict() for r in results] + skipped}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @app.get("/api/projects/{project}/missing")
    async def missing(project: str, refresh: bool = Query(False)):
        cached = app.state.reports.get(project)
        if cached and not refresh and time.time() - cached[0] < REPORT_TTL_SECONDS:
            return cached[1]
        client = get_client()
        since = datetime.now(timezone.utc) - timedelta(days=settings.lookback_days)
        events: list[dict] = []
        scanned = 0
        async for event in client.iter_events(project, settings.max_events_per_project):
            scanned += 1
            created = parse_date(event.get("dateCreated"))
            if created and created < since:
                break
            if event.get("errors"):
                events.append(event)
        entries = collect_missing(events)
        for entry in entries.values():
            try:
                entry.uploaded = bool(await client.find_debug_files(project, entry.debug_id))
            except SentryError:
                entry.uploaded = None
        report = {
            "project": project,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_days": settings.lookback_days,
            "events_scanned": scanned,
            "events_with_errors": len(events),
            "entries": [e.to_dict() for e in sort_entries(entries)],
        }
        app.state.reports[project] = (time.time(), report)
        return report

    return app


app = create_app()
