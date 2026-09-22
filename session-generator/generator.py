#!/usr/bin/env python3
"""Release Health demo data: realistic session traffic across several app versions.

Sends aggregated sessions (the same envelope item the mobile SDKs use in
"request mode") to Relay so Sentry's Releases page shows version adoption,
crash-free sessions and crash-free users per release.

The model is deterministic: every user has a fixed pool position and adopts each
release after a personal delay, so re-running produces the same curves. Relay
accepts sessions up to ~5 days in the past, so the first run backfills the last
BACKFILL_HOURS and every later cycle only sends the minutes since the previous
one (tracked in a state file, so restarts neither duplicate nor lose data).

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

VERSION = "0.1"
HOUR = 3600


@dataclass(frozen=True)
class Release:
    version: str
    released_days_ago: float  # relative to the model's anchor time
    crash_rate: float  # probability that a session crashes
    error_rate: float = 0.02  # probability of a handled error (session "errored")


@dataclass
class Platform:
    project: str
    users: int  # size of the user pool
    sessions_per_hour: int  # at the daily peak
    releases: list[Release] = field(default_factory=list)


def default_platforms() -> list[Platform]:
    """Five releases per app; 8.20.0 ships a crash regression that 8.20.1 fixes."""

    def releases(bundle: str) -> list[Release]:
        return [
            Release(f"{bundle}@8.18.3+818", 30, 0.006),
            Release(f"{bundle}@8.19.0+819", 16, 0.009),
            Release(f"{bundle}@8.19.1+820", 9, 0.005),
            Release(f"{bundle}@8.20.0+821", 4, 0.035, 0.04),
            Release(f"{bundle}@8.20.1+822", 1.5, 0.004),
        ]

    return [
        Platform("android-app", users=3000, sessions_per_hour=260, releases=releases("com.crosstru.finserv.testapp")),
        Platform("ios-app", users=2000, sessions_per_hour=170, releases=releases("com.crosstru.finserv.ios")),
    ]


# -- model ------------------------------------------------------------------------------


def _unit(*parts: object) -> float:
    """Deterministic uniform value in [0, 1) derived from the given parts."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def adoption_delay_days(user: int, release: Release) -> float:
    """How long after a release this user upgrades: mostly days, a long tail of stragglers."""
    u = _unit("adopt", user, release.version)
    return -math.log(1 - u) * 2.2  # exponential, mean 2.2 days


def release_for(user: int, at: datetime, anchor: datetime, releases: list[Release]) -> Release | None:
    """The newest release this user runs at `at`; None before the oldest release existed."""
    ordered = sorted(releases, key=lambda r: -r.released_days_ago)  # oldest first
    if at < anchor - timedelta(days=ordered[0].released_days_ago):
        return None
    current = ordered[0]  # everyone starts on the oldest release
    for release in ordered[1:]:
        upgraded_at = anchor - timedelta(days=release.released_days_ago) + timedelta(days=adoption_delay_days(user, release))
        if at >= upgraded_at:
            current = release
    return current


def diurnal_factor(at: datetime) -> float:
    """Traffic shape for Turkey (UTC+3): quiet at night, busiest in the late afternoon."""
    local_hour = (at.hour + 3) % 24 + at.minute / 60
    if local_hour < 7:
        return 0.12
    return 0.12 + 0.88 * math.sin(math.pi * (local_hour - 7) / 17) ** 1.5


def sessions_for_window(platform: Platform, start: datetime, end: datetime, anchor: datetime) -> dict[str, list[dict]]:
    """Aggregated session buckets for [start, end), keyed by release version."""
    out: dict[str, dict[tuple[str, str], dict]] = {}
    t = start.replace(second=0, microsecond=0)
    if t < start:
        t += timedelta(minutes=1)
    while t < end:
        rng = random.Random(f"{platform.project}|{t.isoformat()}")
        expected = platform.sessions_per_hour / 60 * diurnal_factor(t)
        count = int(expected) + (1 if rng.random() < expected - int(expected) else 0)
        for _ in range(count):
            user = rng.randrange(platform.users)
            release = release_for(user, t, anchor, platform.releases)
            if release is None:
                continue
            roll = rng.random()
            status = "crashed" if roll < release.crash_rate else "errored" if roll < release.crash_rate + release.error_rate else "exited"
            key = (t.strftime("%Y-%m-%dT%H:%M:00Z"), f"user-{platform.project}-{user}")
            bucket = out.setdefault(release.version, {}).setdefault(
                key, {"started": key[0], "did": key[1], "exited": 0, "errored": 0, "crashed": 0}
            )
            bucket[status] += 1
        t += timedelta(minutes=1)
    return {version: list(buckets.values()) for version, buckets in out.items()}


# -- transport ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    project: str
    project_id: str
    public_key: str


def build_envelope(release: str, environment: str, aggregates: list[dict]) -> bytes:
    item = json.dumps({"aggregates": aggregates, "attrs": {"release": release, "environment": environment}},
                      separators=(",", ":")).encode()
    header = json.dumps({"sent_at": datetime.now(timezone.utc).isoformat()}).encode()
    item_header = json.dumps({"type": "sessions", "length": len(item)}).encode()
    return b"\n".join([header, item_header, item, b""])


def send(ingest_url: str, target: Target, envelope: bytes, timeout: float = 30) -> int:
    req = urllib.request.Request(
        f"{ingest_url.rstrip('/')}/api/{target.project_id}/envelope/",
        data=envelope,
        method="POST",
        headers={
            "Content-Type": "application/x-sentry-envelope",
            "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_client=session-generator/{VERSION}, sentry_key={target.public_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def api(base: str, token: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def resolve_targets(api_url: str, token: str, org: str, platforms: list[Platform]) -> dict[str, Target]:
    targets = {}
    for platform in platforms:
        key = api(api_url, token, "GET", f"/api/0/projects/{org}/{platform.project}/keys/")[0]
        targets[platform.project] = Target(platform.project, str(key["projectId"]), key["public"])
    return targets


def ensure_releases(api_url: str, token: str, org: str, platforms: list[Platform], anchor: datetime) -> None:
    """Create any missing release so it appears on the Releases page with a sensible date."""
    for platform in platforms:
        for release in platform.releases:
            released_at = (anchor - timedelta(days=release.released_days_ago)).isoformat()
            try:
                api(api_url, token, "POST", f"/api/0/organizations/{org}/releases/",
                    {"version": release.version, "projects": [platform.project], "dateReleased": released_at})
            except urllib.error.HTTPError as exc:
                if exc.code not in (208, 400, 409):
                    raise


def log(**record) -> None:
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    print(json.dumps(record), flush=True)


def run_window(platforms, targets, start, end, anchor, ingest_url, environment, batch=400) -> dict:
    totals = {"sessions": 0, "crashed": 0, "envelopes": 0, "rejected": 0}
    for platform in platforms:
        for release, buckets in sessions_for_window(platform, start, end, anchor).items():
            for i in range(0, len(buckets), batch):
                chunk = buckets[i : i + batch]
                status = send(ingest_url, targets[platform.project], build_envelope(release, environment, chunk))
                totals["envelopes"] += 1
                if status != 200:
                    totals["rejected"] += 1
                    log(msg="envelope rejected", project=platform.project, release=release, status=status)
                totals["sessions"] += sum(b["exited"] + b["errored"] + b["crashed"] for b in chunk)
                totals["crashed"] += sum(b["crashed"] for b in chunk)
    return totals


def main() -> int:
    env = os.environ.get
    token = env("SESSION_GENERATOR_TOKEN") or env("SYMBOL_PORTAL_SENTRY_TOKEN") or ""
    if not token:
        raise SystemExit("SYMBOL_PORTAL_SENTRY_TOKEN is not set")
    api_url = env("SESSION_GENERATOR_API_URL", "http://web:9000")
    ingest_url = env("SESSION_GENERATOR_INGEST_URL", "http://nginx")
    org = env("SESSION_GENERATOR_ORG") or env("SYMBOL_PORTAL_SENTRY_ORG") or "sentry"
    environment = env("SESSION_GENERATOR_ENVIRONMENT", "production")
    interval = float(env("SESSION_GENERATOR_INTERVAL", "600"))
    backfill_hours = float(env("SESSION_GENERATOR_BACKFILL_HOURS", "108"))  # Relay keeps up to ~5 days
    state_file = env("SESSION_GENERATOR_STATE_FILE", "/data/state.json")

    platforms = default_platforms()
    try:
        with open(state_file) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    now = datetime.now(timezone.utc)
    # The anchor pins the story ("8.20.1 shipped 1.5 days ago") to the first run.
    anchor = datetime.fromisoformat(state.get("anchor") or now.isoformat())
    last = datetime.fromisoformat(state["last_sent"]) if state.get("last_sent") else now - timedelta(hours=backfill_hours)

    targets = resolve_targets(api_url, token, org, platforms)
    ensure_releases(api_url, token, org, platforms, anchor)
    log(msg="session generator starting", anchor=anchor.isoformat(), resume_from=last.isoformat(),
        projects={p: t.project_id for p, t in targets.items()})

    once = "--once" in sys.argv
    while True:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # Walk in hour-sized steps so a long backfill reports progress and saves state often.
        while last < end:
            step_end = min(end, last + timedelta(hours=1))
            totals = run_window(platforms, targets, last, step_end, anchor, ingest_url, environment)
            log(msg="window sent", start=last.isoformat(), end=step_end.isoformat(), **totals)
            if totals["rejected"] and totals["rejected"] == totals["envelopes"]:
                break  # Relay refused everything; retry this window next cycle
            last = step_end
            os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
            with open(state_file, "w") as fh:
                json.dump({"anchor": anchor.isoformat(), "last_sent": last.isoformat()}, fh)
        with open("/tmp/health.txt", "w") as fh:
            fh.write("ok\n")
        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
