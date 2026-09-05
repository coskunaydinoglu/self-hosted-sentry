import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap-mobile-projects.py"
spec = importlib.util.spec_from_file_location("bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class FakeApi(bootstrap.Api):
    def __init__(self, state):
        super().__init__("http://web:9000", "t")
        self.state = state
        self.writes = []

    def request(self, method, path, body=None, params=None):
        if method == "GET":
            if path.endswith("/teams/"):
                return self.state["teams"]
            if path.endswith("/projects/") and "/organizations/" in path:
                return self.state["projects"]
            if path.endswith("/rules/"):
                return self.state["rules"]
            if path.endswith("/alert-rules/"):
                return self.state["metric_rules"]
            if path.endswith("/keys/"):
                return [{"dsn": {"public": "https://k@sentry.local/3"}}]
            return self.state["project_details"]
        self.writes.append((method, path, body))
        if path.endswith("/teams/"):
            return {"id": "42", "slug": body["slug"]}
        if "/projects/" in path and method == "POST":
            return {"id": "9", **body}
        if method == "PUT" and self.state.get("symbol_source_missing"):
            raise bootstrap.ApiError(400, '{"builtinSymbolSources": ["Unknown source"]}', method, path)
        return {}


def test_fresh_org_creates_everything(capsys):
    api = FakeApi({"teams": [], "projects": [], "rules": [], "metric_rules": [], "project_details": {"builtinSymbolSources": ["ios"]}})
    bootstrap.ensure_team(api, "sentry", "mobile")
    for spec in bootstrap.MOBILE_PROJECTS:
        bootstrap.ensure_project(api, "sentry", "mobile", spec)
        bootstrap.ensure_issue_alerts(api, "sentry", spec["slug"], "42")
        bootstrap.enable_system_symbols(api, "sentry", spec["slug"])
    bootstrap.ensure_project(api, "sentry", "mobile", bootstrap.CANARY_PROJECT)
    bootstrap.ensure_canary_alert(api, "sentry", "canary", "42")

    methods = [(m, p.split("/api/0/")[1]) for m, p, _ in api.writes]
    assert methods.count(("POST", "organizations/sentry/teams/")) == 1
    assert methods.count(("POST", "teams/sentry/mobile/projects/")) == 3
    assert methods.count(("POST", "projects/sentry/ios-app/rules/")) == 3
    assert methods.count(("POST", "projects/sentry/android-app/rules/")) == 3
    assert methods.count(("PUT", "projects/sentry/ios-app/")) == 1
    assert methods.count(("POST", "organizations/sentry/alert-rules/")) == 1

    put_body = next(b for m, p, b in api.writes if m == "PUT")
    assert put_body == {"builtinSymbolSources": ["ios", "internal-system-symbols"]}
    metric = next(b for m, p, b in api.writes if p.endswith("/alert-rules/"))
    assert metric["thresholdType"] == 1 and metric["triggers"][0]["alertThreshold"] == 1
    assert metric["projects"] == ["canary"]


def test_rerun_is_idempotent():
    rules = [{"name": r["name"]} for r in bootstrap.issue_alert_payloads("42")]
    api = FakeApi({
        "teams": [{"id": "42", "slug": "mobile"}],
        "projects": [{"slug": s["slug"]} for s in bootstrap.MOBILE_PROJECTS + [bootstrap.CANARY_PROJECT]],
        "rules": rules,
        "metric_rules": [{"name": bootstrap.canary_metric_alert_payload("42", "canary")["name"]}],
        "project_details": {"builtinSymbolSources": ["ios", "internal-system-symbols"]},
    })
    bootstrap.ensure_team(api, "sentry", "mobile")
    for spec in bootstrap.MOBILE_PROJECTS:
        bootstrap.ensure_project(api, "sentry", "mobile", spec)
        bootstrap.ensure_issue_alerts(api, "sentry", spec["slug"], "42")
        bootstrap.enable_system_symbols(api, "sentry", spec["slug"])
    bootstrap.ensure_canary_alert(api, "sentry", "canary", "42")
    assert api.writes == []


def test_missing_symbol_source_is_skipped_not_fatal(capsys):
    api = FakeApi({"teams": [], "projects": [], "rules": [], "metric_rules": [],
                   "project_details": {"builtinSymbolSources": ["ios"]}, "symbol_source_missing": True})
    bootstrap.enable_system_symbols(api, "sentry", "ios-app")
    assert "not registered in sentry.conf.py" in capsys.readouterr().out


def test_dry_run_performs_no_writes(monkeypatch, capsys):
    import urllib.request

    def fake_urlopen(req, timeout):
        assert req.get_method() == "GET"
        import io
        import urllib.error

        # project-scoped GETs 404 because nothing was really created in dry-run
        if "/projects/sentry/" in req.full_url or "alert-rules" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"detail":"not found"}'))

        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(json.dumps([]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rc = bootstrap.main(["--url", "http://web:9000", "--token", "t", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] POST /api/0/organizations/sentry/teams/" in out
    assert out.count("[dry-run] POST /api/0/teams/sentry/mobile/projects/") == 3


def test_issue_alert_payload_shape():
    payloads = bootstrap.issue_alert_payloads("7")
    assert [p["name"] for p in payloads] == ["New crash type", "Crash regressed", "Crash spike (>100/h)"]
    for p in payloads:
        assert p["actions"][0]["targetIdentifier"] == "7" and p["owner"] == "team:7"
        assert p["conditions"][0]["id"].startswith("sentry.rules.conditions.")
