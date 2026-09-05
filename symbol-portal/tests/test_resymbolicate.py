import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.resymbolicate import build_request, collect_stacktraces, native_sources, summarize
from app.sentry_api import SymbolicatorClient
from tests.test_api import settings

DEBUG_ID = "c0ffee00-1111-2222-3333-444455556666"
EVENT = {
    "event_id": "ab" * 16,
    "platform": "cocoa",
    "contexts": {"os": {"name": "iOS", "version": "17.5"}},
    "debug_meta": {
        "images": [
            {"type": "macho", "debug_id": DEBUG_ID, "code_file": "/private/var/containers/MyApp.app/MyApp",
             "image_addr": "0x100000000", "image_size": 65536, "arch": "arm64"},
            {"type": "macho", "debug_id": "11111111-2222-3333-4444-555555555555", "code_file": "/usr/lib/system/libsystem_kernel.dylib",
             "image_addr": 0x1a0000000, "image_size": 4096},
            {"type": "proguard", "uuid": "not-native"},
        ]
    },
    "exception": {"values": [{
        "type": "EXC_BAD_ACCESS", "value": "KERN_INVALID_ADDRESS",
        "mechanism": {"type": "mach", "meta": {"signal": {"number": 11}}},
        "stacktrace": {"registers": {"pc": "0x100001000"}, "frames": [
            {"instruction_addr": "0x1a0000010", "package": "/usr/lib/system/libsystem_kernel.dylib"},
            {"instruction_addr": 0x100001000, "package": "/private/var/containers/MyApp.app/MyApp"},
        ]},
    }]},
    "threads": {"values": [
        {"id": 0, "crashed": True, "stacktrace": {"frames": [{"instruction_addr": "0x100001000"}]}},
        {"id": 5, "name": "worker", "stacktrace": {"frames": [{"instruction_addr": "0x100002000"}]}},
        {"id": 6, "stacktrace": {"frames": [{"function": "java_only"}]}},
    ]},
}

SYMBOLICATOR_RESPONSE = {
    "status": "completed",
    "stacktraces": [
        {"frames": [
            {"status": "missing", "instruction_addr": "0x1a0000010", "package": "/usr/lib/system/libsystem_kernel.dylib"},
            {"status": "symbolicated", "instruction_addr": "0x100001000", "function": "-[CrashViewController tap:]",
             "filename": "CrashViewController.swift", "lineno": 42, "package": "/private/var/containers/MyApp.app/MyApp", "in_app": True},
        ]},
        {"frames": [{"status": "symbolicated", "instruction_addr": "0x100002000", "function": "worker_main"}]},
    ],
    "modules": [
        {"debug_id": DEBUG_ID, "code_file": "/private/var/containers/MyApp.app/MyApp", "debug_status": "found", "arch": "arm64"},
        {"debug_id": "11111111-2222-3333-4444-555555555555", "code_file": "/usr/lib/system/libsystem_kernel.dylib", "debug_status": "missing"},
    ],
}


def test_collect_stacktraces_skips_crashed_thread_duplicate_and_non_native():
    traces = collect_stacktraces(EVENT)
    assert [t["label"] for t in traces] == ["exception 0: EXC_BAD_ACCESS KERN_INVALID_ADDRESS", "thread 5 worker"]


def test_build_request_normalises_addresses_and_filters_images():
    sources = native_sources("http://web:9000/", "sentry", "ios-app", "tok")
    request = build_request(EVENT, sources)
    assert request["sources"][0] == {
        "id": "sentry:project", "type": "sentry",
        "url": "http://web:9000/api/0/projects/sentry/ios-app/files/dsyms/", "token": "tok",
    }
    assert [m["type"] for m in request["modules"]] == ["macho", "macho"]
    assert request["modules"][1]["image_addr"] == "0x1a0000000"
    assert request["stacktraces"][0]["frames"][1] == {"instruction_addr": "0x100001000"}
    assert request["stacktraces"][0]["registers"] == {"pc": "0x100001000"}
    assert request["signal"] == 11
    assert request["os"] == {"name": "iOS", "version": "17.5"}


def test_build_request_returns_none_without_native_data():
    assert build_request({"exception": {"values": [{"stacktrace": {"frames": [{"function": "a"}]}}]}}, []) is None
    assert build_request({"debug_meta": {"images": [{"type": "macho", "debug_id": DEBUG_ID}]}, "exception": {"values": []}}, []) is None


def test_summarize_counts_and_missing_ids():
    summary = summarize(EVENT, SYMBOLICATOR_RESPONSE)
    assert summary["frames_total"] == 3 and summary["frames_symbolicated"] == 2
    assert summary["missing_debug_ids"] == ["11111111-2222-3333-4444-555555555555"]
    assert summary["stacktraces"][0]["label"].startswith("exception 0")
    assert summary["stacktraces"][0]["frames"][1]["package"] == "MyApp"
    assert summary["stacktraces"][1]["label"] == "thread 5 worker"
    assert summary["modules"][0]["code_file"] == "MyApp"


def test_symbolicate_endpoint_polls_pending_requests(tmp_path, fake, client):
    calls = []

    def symbolicator(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/symbolicate":
            body = request.read()
            assert b'"sentry:internal-system-symbols"' in body and b'"token":"test-token"' in body
            return httpx.Response(200, json={"status": "pending", "request_id": "req-1", "retry_after": 1})
        if request.url.path == "/requests/req-1":
            return httpx.Response(200, json=SYMBOLICATOR_RESPONSE)
        return httpx.Response(404)

    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/" + "ab" * 16 + "/json/"):
            return httpx.Response(200, json=EVENT)
        return original(request)

    fake.handler = handler
    sym = SymbolicatorClient("http://symbolicator:3021", client=httpx.AsyncClient(transport=httpx.MockTransport(symbolicator)))
    app = create_app(settings(tmp_path), client=client, symbolicator=sym)
    with TestClient(app) as tc:
        resp = tc.post("/api/projects/ios-app/events/" + "ab" * 16 + "/symbolicate")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frames_symbolicated"] == 2 and data["stored_event_unchanged"] is True
    assert calls == [("POST", "/symbolicate"), ("GET", "/requests/req-1")]


def test_symbolicate_endpoint_rejects_non_native_event(tmp_path, fake, client):
    original = fake.handler
    fake.handler = lambda r: httpx.Response(200, json={"platform": "java", "exception": {"values": []}}) if "/json/" in r.url.path else original(r)
    app = create_app(settings(tmp_path), client=client, symbolicator=SymbolicatorClient("http://symbolicator:3021"))
    with TestClient(app) as tc:
        resp = tc.post("/api/projects/android-app/events/" + "cd" * 16 + "/symbolicate")
    assert resp.status_code == 422
    assert "ProGuard" in resp.json()["detail"]
