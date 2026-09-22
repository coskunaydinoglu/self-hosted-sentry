import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generator as g  # noqa: E402

ANCHOR = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
ANDROID = g.default_platforms()[0]


def share(version_suffix, at, users=2000):
    hits = sum(
        1 for u in range(users)
        if (r := g.release_for(u, at, ANCHOR, ANDROID.releases)) and r.version.endswith(version_suffix)
    )
    return hits / users


def test_newest_release_adoption_grows_over_time():
    newest = "8.20.1+822"
    at_release = ANCHOR - timedelta(days=1.5)
    assert share(newest, at_release - timedelta(hours=1)) == 0
    now, later = share(newest, ANCHOR), share(newest, ANCHOR + timedelta(days=4))
    assert 0.3 < now < later < 1.0


def test_everyone_on_some_release_and_nobody_before_the_oldest():
    assert g.release_for(1, ANCHOR - timedelta(days=31), ANCHOR, ANDROID.releases) is None
    assert all(g.release_for(u, ANCHOR, ANCHOR, ANDROID.releases) for u in range(500))


def test_regression_release_crashes_more():
    window = sessions = {}
    start = ANCHOR - timedelta(days=3)
    sessions = g.sessions_for_window(ANDROID, start, start + timedelta(hours=24), ANCHOR)

    def crash_free(version):
        total = sum(b["exited"] + b["errored"] + b["crashed"] for b in sessions[version])
        crashed = sum(b["crashed"] for b in sessions[version])
        return 1 - crashed / total

    bad = crash_free("com.crosstru.finserv.testapp@8.20.0+821")
    good = crash_free("com.crosstru.finserv.testapp@8.19.1+820")
    assert bad < 0.985 < good


def test_windows_are_deterministic_and_split_cleanly():
    start = ANCHOR - timedelta(hours=5)
    whole = g.sessions_for_window(ANDROID, start, start + timedelta(hours=2), ANCHOR)
    first = g.sessions_for_window(ANDROID, start, start + timedelta(hours=1), ANCHOR)
    second = g.sessions_for_window(ANDROID, start + timedelta(hours=1), start + timedelta(hours=2), ANCHOR)
    assert whole == g.sessions_for_window(ANDROID, start, start + timedelta(hours=2), ANCHOR)
    count = lambda d: sum(b["exited"] + b["errored"] + b["crashed"] for v in d.values() for b in v)  # noqa: E731
    assert count(whole) == count(first) + count(second) > 0
    for buckets in whole.values():
        for b in buckets:
            assert b["started"].endswith(":00Z") and b["did"].startswith("user-android-app-")


def test_envelope_shape():
    env = g.build_envelope("app@1.0+1", "production", [{"started": "2026-09-22T10:00:00Z", "did": "u", "exited": 2, "errored": 0, "crashed": 1}])
    header, item_header, item, trailer = env.split(b"\n")
    assert json.loads(item_header) == {"type": "sessions", "length": len(item)}
    body = json.loads(item)
    assert body["attrs"] == {"release": "app@1.0+1", "environment": "production"}
    assert body["aggregates"][0]["crashed"] == 1 and trailer == b""


def test_run_window_counts_and_reports_rejections(monkeypatch):
    sent = []
    monkeypatch.setattr(g, "send", lambda url, target, env: sent.append(env) or 200)
    targets = {p.project: g.Target(p.project, "3", "k") for p in g.default_platforms()}
    start = ANCHOR - timedelta(hours=1)
    totals = g.run_window(g.default_platforms(), targets, start, ANCHOR, ANCHOR, "http://nginx", "production", batch=50)
    assert totals["sessions"] > 0 and totals["rejected"] == 0 and totals["envelopes"] == len(sent)
    monkeypatch.setattr(g, "send", lambda url, target, env: 400)
    bad = g.run_window(g.default_platforms(), targets, start, ANCHOR, ANCHOR, "http://nginx", "production")
    assert bad["rejected"] == bad["envelopes"] > 0
