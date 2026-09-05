"""A small in-memory fake of the Sentry endpoints the portal talks to."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.sentry_api import SentryClient

PUBLIC_URL = "https://sentry.example.com"
INTERNAL_URL = "http://web:9000"


class FakeSentry:
    def __init__(self):
        self.chunks: dict[str, bytes] = {}
        self.difs: dict[str, dict] = {}  # debug_id -> serialized dif
        self.assembled: dict[str, int] = {}  # total sha -> assemble call count
        self.archives: list[list[str]] = []  # names inside zips posted to the legacy endpoint
        self.events: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.chunk_options = {
            "url": f"{PUBLIC_URL}/api/0/organizations/sentry/chunk-upload/",
            "chunkSize": 4,
            "chunksPerRequest": 2,
            "maxFileSize": 1024 * 1024,
            "maxRequestSize": 1024,
            "concurrency": 1,
            "hashAlgorithm": "sha1",
        }
        self.pending_polls = 1  # assemble returns "assembling" this many times before "ok"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlsplit(str(request.url)).path
        query = parse_qs(urlsplit(str(request.url)).query)
        assert request.headers.get("Authorization") == "Bearer test-token"

        if path == "/api/0/organizations/sentry/projects/":
            return httpx.Response(200, json=[{"slug": "ios-app", "name": "iOS App", "platform": "apple-ios", "id": "1"}])
        if path == "/api/0/organizations/sentry/chunk-upload/":
            if request.method == "GET":
                return httpx.Response(200, json=self.chunk_options)
            assert request.url.host == "web", "chunk uploads must go to the internal host"
            for name, data in _multipart_files(request):
                assert hashlib.sha1(data).hexdigest() == name
                self.chunks[name] = data
            return httpx.Response(200, json={})
        m = re.fullmatch(r"/api/0/projects/sentry/([^/]+)/files/difs/assemble/", path)
        if m and request.method == "POST":
            return httpx.Response(200, json=self._assemble(json.loads(request.content)))
        m = re.fullmatch(r"/api/0/projects/sentry/([^/]+)/files/dsyms/", path)
        if m and request.method == "POST":
            import io
            import zipfile

            (_, data), = _multipart_files(request)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
            self.archives.append(names)
            uuid = names[0].split("/")[-1].removesuffix(".txt")
            dif = {"id": "9", "debugId": uuid, "uuid": uuid, "objectName": "proguard-mapping", "cpuName": "any"}
            self.difs[uuid] = dif
            return httpx.Response(201, json=[dif])
        if m and request.method == "GET":
            if "debug_id" in query:
                dif = self.difs.get(query["debug_id"][0].lower())
                return httpx.Response(200, json=[dif] if dif else [])
            return httpx.Response(200, json=list(self.difs.values()))
        m = re.fullmatch(r"/api/0/projects/sentry/([^/]+)/events/", path)
        if m:
            assert query.get("full") == ["true"] or "cursor" in query
            cursor = int(query.get("cursor", ["0"])[0])
            page = self.events[cursor : cursor + 2]
            headers = {}
            nxt = cursor + 2
            has_more = nxt < len(self.events)
            headers["Link"] = (
                f'<{INTERNAL_URL}{path}?full=true&cursor={nxt}>; rel="next"; results="{"true" if has_more else "false"}"; cursor="{nxt}"'
            )
            return httpx.Response(200, json=page, headers=headers)
        return httpx.Response(404, json={"detail": f"unhandled {request.method} {path}"})

    def _assemble(self, payload: dict) -> dict:
        out = {}
        for total_sha, spec in payload.items():
            missing = [c for c in spec["chunks"] if c not in self.chunks]
            if missing:
                out[total_sha] = {"state": "not_found", "missingChunks": missing}
                continue
            self.assembled[total_sha] = self.assembled.get(total_sha, 0) + 1
            if self.assembled[total_sha] <= self.pending_polls:
                out[total_sha] = {"state": "assembling", "missingChunks": []}
                continue
            data = b"".join(self.chunks[c] for c in spec["chunks"])
            assert hashlib.sha1(data).hexdigest() == total_sha
            debug_id = "11111111-2222-3333-4444-" + total_sha[:12]
            dif = {"id": "5", "debugId": debug_id, "uuid": debug_id, "objectName": spec["name"], "cpuName": "arm64"}
            self.difs[debug_id] = dif
            out[total_sha] = {"state": "ok", "missingChunks": [], "dif": dif}
        return out


def _multipart_files(request: httpx.Request) -> list[tuple[str, bytes]]:
    content_type = request.headers["Content-Type"]
    boundary = content_type.split("boundary=")[1].encode()
    files = []
    for part in request.content.split(b"--" + boundary):
        if b"filename=" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        name = re.search(rb'filename="([^"]+)"', head).group(1).decode()
        files.append((name, body.rstrip(b"\r\n")))
    return files


@pytest.fixture
def fake():
    return FakeSentry()


@pytest.fixture
def client(fake):
    http = httpx.AsyncClient(
        # late-bound so tests can swap `fake.handler` after the client exists
        transport=httpx.MockTransport(lambda request: fake.handler(request)),
        base_url=INTERNAL_URL,
        headers={"Authorization": "Bearer test-token"},
    )
    return SentryClient(INTERNAL_URL, "sentry", "test-token", client=http)
