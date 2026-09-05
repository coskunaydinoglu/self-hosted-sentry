import base64
import zipfile

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_difs import MACHO, MAPPING, make_dsym_zip


def settings(tmp_path, **overrides):
    base = dict(
        sentry_url="http://web:9000", sentry_org="sentry", sentry_token="test-token", basic_auth=None,
        lookback_days=14, max_events_per_project=100, rewrite_chunk_url=True, tmp_dir=str(tmp_path / "tmp"),
        symbolicator_url="http://symbolicator:3021", system_symbols_url="http://symbol-server/",
    )
    base.update(overrides)
    return Settings(**base)


def test_upload_endpoint_end_to_end(tmp_path, fake, client):
    app = create_app(settings(tmp_path), client=client)
    archive = make_dsym_zip(tmp_path / "MyApp.dSYM.zip")
    with TestClient(app) as tc:
        assert tc.get("/api/projects").json()[0]["slug"] == "ios-app"
        with open(archive, "rb") as fh:
            resp = tc.post(
                "/api/projects/ios-app/upload",
                files=[("files", ("MyApp.dSYM.zip", fh, "application/zip")), ("files", ("mapping.txt", MAPPING, "text/plain")), ("files", ("empty.zip", b"", "application/zip"))],
                data={"proguard_uuid": "8a5f3c2e-1b4d-4e6f-9a0b-1c2d3e4f5a6b"},
            )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    by_status = sorted((r["status"], r.get("kind")) for r in results)
    assert by_status == [("skipped", None), ("uploaded", "native"), ("uploaded", "native"), ("uploaded", "proguard")]
    assert fake.archives == [["proguard/8a5f3c2e-1b4d-4e6f-9a0b-1c2d3e4f5a6b.txt"]]
    # temp files were cleaned up
    assert not any(p.name.startswith("upload-") for p in (tmp_path / "tmp").iterdir())


def test_missing_report_marks_uploaded_symbols(tmp_path, fake, client):
    missing_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    fake.difs["11111111-2222-3333-4444-555555555555"] = {"debugId": "11111111-2222-3333-4444-555555555555"}
    fake.events = [
        {"eventID": "e1", "dateCreated": "2099-01-01T00:00:00Z", "release": {"version": "2.0"}, "errors": [
            {"type": "native_missing_dsym", "data": {"image_uuid": missing_id, "image_path": "/App"}}]},
        {"eventID": "e2", "dateCreated": "2099-01-01T00:00:00Z", "errors": [
            {"type": "native_missing_dsym", "data": {"image_uuid": "11111111-2222-3333-4444-555555555555"}}]},
        {"eventID": "e3", "dateCreated": "2099-01-01T00:00:00Z", "errors": []},
        {"eventID": "old", "dateCreated": "2000-01-01T00:00:00Z", "errors": [
            {"type": "native_missing_dsym", "data": {"image_uuid": "too-old"}}]},
    ]
    app = create_app(settings(tmp_path), client=client)
    with TestClient(app) as tc:
        report = tc.get("/api/projects/ios-app/missing").json()
        assert report["events_scanned"] == 4  # stopped at the first event older than the window
        assert {e["debug_id"]: e["uploaded"] for e in report["entries"]} == {
            missing_id: False,
            "11111111-2222-3333-4444-555555555555": True,
        }
        assert report["entries"][0]["releases"] in (["2.0"], [])
        # second call is served from cache: no new event requests
        before = len(fake.requests)
        tc.get("/api/projects/ios-app/missing")
        assert len(fake.requests) == before


def test_basic_auth_guard(tmp_path, fake, client):
    app = create_app(settings(tmp_path, basic_auth="dev:secret"), client=client)
    with TestClient(app) as tc:
        assert tc.get("/api/health").status_code == 200  # health stays open for docker healthcheck
        assert tc.get("/api/projects").status_code == 401
        token = base64.b64encode(b"dev:secret").decode()
        assert tc.get("/api/projects", headers={"Authorization": f"Basic {token}"}).status_code == 200
        assert tc.get("/", headers={"Authorization": f"Basic {token}"}).text.startswith("<!doctype html>")


def test_unconfigured_portal_reports_503(tmp_path):
    app = create_app(settings(tmp_path, sentry_token=""))
    with TestClient(app) as tc:
        assert tc.get("/api/health").json()["configured"] is False
        assert tc.get("/api/projects").status_code == 503
