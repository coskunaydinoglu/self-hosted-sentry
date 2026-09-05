#!/usr/bin/env python3
"""Create the mobile projects, alert rules and symbol-source settings in Sentry.

Idempotent: re-running only adds what is missing. Standard library only.

    SENTRY_URL=https://sentry.example.com SENTRY_AUTH_TOKEN=... \\
        ./scripts/bootstrap-mobile-projects.py --org sentry [--team mobile] [--dry-run]

Creates (unless present):
  * team          <team>
  * projects      ios-app (apple-ios), android-app (android), canary (other)
  * issue alerts  on ios-app / android-app: new issue, regression, spike (>100 events/h)
  * metric alert  on canary: no events for 15 minutes (ingest path is broken)
  * symbol source enables "internal-system-symbols" on the mobile projects when
                  sentry.conf.py registers it (see symbol-server/README.md)

Notification target is the team's members by e-mail; swap the action for a
Teams/Slack integration action once the integration is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MOBILE_PROJECTS = [
    {"slug": "ios-app", "name": "iOS App", "platform": "apple-ios"},
    {"slug": "android-app", "name": "Android App", "platform": "android"},
]
CANARY_PROJECT = {"slug": "canary", "name": "Ingest Canary", "platform": "other"}
SYSTEM_SYMBOL_SOURCE = "internal-system-symbols"


class Api:
    def __init__(self, base_url: str, token: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.dry_run = dry_run

    def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if self.dry_run and method != "GET":
            print(f"  [dry-run] {method} {path} {json.dumps(body) if body else ''}")
            return {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise ApiError(exc.code, detail, method, path) from None

    def get(self, path, default=None, **params):
        """GET; in dry-run mode a 404 (project not really created) yields `default`."""
        try:
            return self.request("GET", path, params=params or None)
        except ApiError as exc:
            if self.dry_run and exc.status == 404 and default is not None:
                return default
            raise

    def post(self, path, body):
        return self.request("POST", path, body)

    def put(self, path, body):
        return self.request("PUT", path, body)


class ApiError(Exception):
    def __init__(self, status: int, detail: str, method: str, path: str):
        super().__init__(f"{method} {path} -> {status}: {detail[:400]}")
        self.status = status
        self.detail = detail


# -- payload builders (kept pure so they can be unit-tested) --------------------

def issue_alert_payloads(team_id: str) -> list[dict]:
    email = {"id": "sentry.mail.actions.NotifyEmailAction", "targetType": "Team", "targetIdentifier": str(team_id),
             "fallthroughType": "ActiveMembers"}
    base = {"actionMatch": "any", "filterMatch": "all", "environment": None, "owner": f"team:{team_id}",
            "actions": [email], "filters": []}
    return [
        {**base, "name": "New crash type", "frequency": 30,
         "conditions": [{"id": "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition"}]},
        {**base, "name": "Crash regressed", "frequency": 30,
         "conditions": [{"id": "sentry.rules.conditions.regression_event.RegressionEventCondition"}]},
        {**base, "name": "Crash spike (>100/h)", "frequency": 60,
         "conditions": [{"id": "sentry.rules.conditions.event_frequency.EventFrequencyCondition",
                         "value": 100, "interval": "1h", "comparisonType": "count"}]},
    ]


def canary_metric_alert_payload(team_id: str, project_slug: str) -> dict:
    return {
        "name": "Ingest canary silent (crash ingest path broken)",
        "dataset": "events",
        "queryType": 0,
        "eventTypes": ["error", "default"],
        "query": "",
        "aggregate": "count()",
        "timeWindow": 15,
        "thresholdType": 1,  # below
        "resolveThreshold": None,
        "projects": [project_slug],
        "environment": None,
        "owner": f"team:{team_id}",
        "triggers": [
            {"label": "critical", "alertThreshold": 1,
             "actions": [{"type": "email", "targetType": "team", "targetIdentifier": str(team_id)}]},
        ],
    }


# -- steps ----------------------------------------------------------------------

def ensure_team(api: Api, org: str, slug: str) -> dict:
    for team in api.get(f"/api/0/organizations/{org}/teams/"):
        if team["slug"] == slug:
            print(f"team {slug}: exists")
            return team
    print(f"team {slug}: creating")
    return api.post(f"/api/0/organizations/{org}/teams/", {"name": slug, "slug": slug}) or {"slug": slug, "id": "0"}


def ensure_project(api: Api, org: str, team: str, spec: dict) -> dict:
    existing = {p["slug"]: p for p in api.get(f"/api/0/organizations/{org}/projects/")}
    if spec["slug"] in existing:
        print(f"project {spec['slug']}: exists")
        return existing[spec["slug"]]
    print(f"project {spec['slug']}: creating ({spec['platform']})")
    return api.post(f"/api/0/teams/{org}/{team}/projects/", spec) or spec


def ensure_issue_alerts(api: Api, org: str, project: str, team_id: str) -> None:
    existing = {r["name"] for r in api.get(f"/api/0/projects/{org}/{project}/rules/", default=[])}
    for payload in issue_alert_payloads(team_id):
        if payload["name"] in existing:
            print(f"  alert '{payload['name']}': exists")
            continue
        print(f"  alert '{payload['name']}': creating")
        api.post(f"/api/0/projects/{org}/{project}/rules/", payload)


def ensure_canary_alert(api: Api, org: str, project: str, team_id: str) -> None:
    payload = canary_metric_alert_payload(team_id, project)
    existing = {r["name"] for r in api.get(f"/api/0/organizations/{org}/alert-rules/", default=[], project=project)}
    if payload["name"] in existing:
        print(f"  metric alert '{payload['name']}': exists")
        return
    print(f"  metric alert '{payload['name']}': creating")
    try:
        api.post(f"/api/0/organizations/{org}/alert-rules/", payload)
    except ApiError as exc:
        print(f"  ! could not create the metric alert ({exc}).\n"
              f"    Create it by hand: Alerts > Create Alert > Number of Errors, project {project},\n"
              f"    'count() is below 1 in 15 minutes'.")


def enable_system_symbols(api: Api, org: str, project: str) -> None:
    details = api.get(f"/api/0/projects/{org}/{project}/", default={})
    if not isinstance(details, dict):
        details = {}
    sources = list(details.get("builtinSymbolSources") or [])
    if SYSTEM_SYMBOL_SOURCE in sources:
        print(f"  symbol source {SYSTEM_SYMBOL_SOURCE}: enabled")
        return
    sources.append(SYSTEM_SYMBOL_SOURCE)
    try:
        api.put(f"/api/0/projects/{org}/{project}/", {"builtinSymbolSources": sources})
        print(f"  symbol source {SYSTEM_SYMBOL_SOURCE}: enabled now")
    except ApiError as exc:
        if exc.status == 400:
            print(f"  symbol source {SYSTEM_SYMBOL_SOURCE}: not registered in sentry.conf.py yet, skipped "
                  f"(copy the SENTRY_BUILTIN_SOURCES block from sentry.conf.example.py)")
        else:
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("SENTRY_URL"), help="Sentry base URL (or SENTRY_URL)")
    parser.add_argument("--token", default=os.environ.get("SENTRY_AUTH_TOKEN"), help="org auth token (or SENTRY_AUTH_TOKEN)")
    parser.add_argument("--org", default=os.environ.get("SENTRY_ORG", "sentry"))
    parser.add_argument("--team", default="mobile")
    parser.add_argument("--dry-run", action="store_true", help="print writes instead of performing them")
    args = parser.parse_args(argv)
    if not args.url or not args.token:
        parser.error("SENTRY_URL and SENTRY_AUTH_TOKEN (or --url/--token) are required")

    api = Api(args.url, args.token, dry_run=args.dry_run)
    try:
        team = ensure_team(api, args.org, args.team)
        team_id = str(team.get("id", "0"))
        for spec in MOBILE_PROJECTS:
            ensure_project(api, args.org, args.team, spec)
            ensure_issue_alerts(api, args.org, spec["slug"], team_id)
            enable_system_symbols(api, args.org, spec["slug"])
        canary = ensure_project(api, args.org, args.team, CANARY_PROJECT)
        ensure_canary_alert(api, args.org, CANARY_PROJECT["slug"], team_id)
        if not args.dry_run:
            keys = api.get(f"/api/0/projects/{args.org}/{CANARY_PROJECT['slug']}/keys/")
            if keys:
                dsn = keys[0]["dsn"]["public"]
                print(f"\ncanary DSN (replace the host with your PUBLIC ingest hostname):\n  INGEST_CANARY_DSN={dsn}")
        _ = canary
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
