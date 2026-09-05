import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canary  # noqa: E402


def test_dsn_parse_public_host_and_project():
    dsn = canary.Dsn.parse("https://abc123@crash-ingest.example.com/7")
    assert dsn.envelope_url == "https://crash-ingest.example.com/api/7/envelope/"
    assert "sentry_key=abc123" in dsn.auth_header
    assert dsn.project_id == "7"


def test_dsn_parse_with_port_and_path_prefix():
    dsn = canary.Dsn.parse("http://key@localhost:9000/sentry/12")
    assert dsn.envelope_url == "http://localhost:9000/sentry/api/12/envelope/"


@pytest.mark.parametrize("bad", ["", "not a dsn", "https://host/1", "https://key@host/"])
def test_dsn_rejects_garbage(bad):
    with pytest.raises(ValueError):
        canary.Dsn.parse(bad)


def test_event_is_padded_to_requested_size_and_tagged():
    event = canary.build_event("e" * 32, payload_kb=64, probe=False, release="r", environment="canary")
    encoded = json.dumps(event)
    assert 64 * 1024 <= len(encoded) <= 64 * 1024 + 4096
    assert event["tags"]["canary"] == "true"
    assert event["fingerprint"] == ["ingest-canary"]
    assert "${jndi" not in encoded


def test_probe_strings_only_when_enabled():
    event = canary.build_event("e" * 32, payload_kb=8, probe=True, release="r", environment="canary")
    encoded = json.dumps(event)
    assert "${jndi:ldap://example.invalid/a}" in encoded
    assert event["tags"]["canary_probe"] == "true"


def test_envelope_has_three_lines_and_matching_length():
    dsn = canary.Dsn.parse("https://k@h/1")
    event = canary.build_event("f" * 32, payload_kb=1, probe=False, release="r", environment="e")
    envelope = canary.build_envelope(dsn, event)
    header, item, body, trailer = envelope.split(b"\n")
    assert json.loads(header)["event_id"] == "f" * 32
    item_header = json.loads(item)
    assert item_header["type"] == "event"
    assert item_header["length"] == len(body)
    assert json.loads(body)["event_id"] == "f" * 32
    assert trailer == b""
    assert gzip.decompress(gzip.compress(envelope)) == envelope


def test_send_envelope_reports_http_errors(monkeypatch):
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout):
        assert req.get_header("X-sentry-auth").startswith("Sentry sentry_version=7")
        assert req.get_header("Content-encoding") == "gzip"
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, __import__("io").BytesIO(b"blocked by WAF"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dsn = canary.Dsn.parse("https://k@h/1")
    status, body = canary.send_envelope(dsn, b"x", use_gzip=True, timeout=1)
    assert (status, body) == (403, "blocked by WAF")


def test_run_once_classifies_stages(monkeypatch):
    dsn = canary.Dsn.parse("https://k@h/1")
    cfg = {
        "dsn": dsn, "payload_kb": 1, "probe": False, "release": "r", "environment": "e", "http_timeout": 1,
        "token": "t", "api_url": "http://web:9000", "org": "sentry", "project": "canary",
        "verify_timeout": 0.05, "poll_interval": 0.01,
    }
    statsd = canary.Statsd(None)

    monkeypatch.setattr(canary, "send_envelope", lambda *a, **k: (403, "blocked"))
    result = canary.run_once(cfg, statsd, use_gzip=True)
    assert result["stage"] == "ingest" and result["delivered"] is False and result["http_status"] == 403

    monkeypatch.setattr(canary, "send_envelope", lambda *a, **k: (200, '{"id":"x"}'))
    monkeypatch.setattr(canary, "event_arrived", lambda *a, **k: False)
    result = canary.run_once(cfg, statsd, use_gzip=False)
    assert result["stage"] == "accepted_not_stored"

    monkeypatch.setattr(canary, "event_arrived", lambda *a, **k: True)
    result = canary.run_once(cfg, statsd, use_gzip=False)
    assert result["stage"] == "delivered" and result["delivered"] is True and result["verify_ms"] is not None

    cfg["token"] = ""
    result = canary.run_once(cfg, statsd, use_gzip=True)
    assert result["stage"] == "unverified"
