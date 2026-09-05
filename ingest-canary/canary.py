#!/usr/bin/env python3
"""Ingest canary: prove that crash reports make it through WAF/proxies into Sentry.

Every INTERVAL seconds it sends a synthetic error event with a large stack trace
through the *public* DSN (the same path the mobile SDKs use), then asks Sentry's
API over the internal network whether the event arrived. Delivery rate and
latency go to the log, an optional statsd server and a state file. A missing
event is the "are we losing crashes to the WAF?" signal, measured instead of
suspected.

Standard library only, so the image stays tiny.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

VERSION = "0.1"

# Fragments that commonly trip WAF signature rules (Log4Shell, SQLi, XSS) and
# that legitimately appear in real stack traces, log breadcrumbs and user input.
# Only sent when INGEST_CANARY_PROBE_STRINGS=1, so you can measure whether your
# WAF drops crash reports that merely *contain* such text.
PROBE_STRINGS = [
    "${jndi:ldap://example.invalid/a}",
    "' OR 1=1 --",
    "<script>alert(1)</script>",
    "../../etc/passwd",
    "SELECT * FROM users WHERE id = 1",
]


@dataclass(frozen=True)
class Dsn:
    scheme: str
    host: str
    public_key: str
    project_id: str
    path: str = ""

    @classmethod
    def parse(cls, raw: str) -> "Dsn":
        parts = urllib.parse.urlsplit(raw.strip())
        if not parts.scheme or not parts.hostname or not parts.username or not parts.path.strip("/"):
            raise ValueError("DSN must look like https://<key>@host/<project_id>")
        path, _, project_id = parts.path.rstrip("/").rpartition("/")
        netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
        return cls(scheme=parts.scheme, host=netloc, public_key=parts.username, project_id=project_id, path=path)

    @property
    def envelope_url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}/api/{self.project_id}/envelope/"

    @property
    def auth_header(self) -> str:
        return f"Sentry sentry_version=7, sentry_client=ingest-canary/{VERSION}, sentry_key={self.public_key}"


def build_event(event_id: str, payload_kb: int, probe: bool, release: str, environment: str) -> dict:
    """An error event whose stack trace is padded to roughly payload_kb kilobytes."""
    frames = []
    size = 0
    index = 0
    probes = PROBE_STRINGS if probe else []
    while size < payload_kb * 1024:
        frame = {
            "function": f"-[CanaryViewController method{index:04d}:withArgument:]",
            "module": "IngestCanary",
            "filename": f"Sources/Canary/Feature{index % 17:02d}/CanaryViewController.swift",
            "lineno": 100 + index,
            "in_app": True,
            "vars": {"argument": f"synthetic-{index}-" + "x" * 64},
        }
        if probes:
            frame["vars"]["user_input"] = probes[index % len(probes)]
        frames.append(frame)
        size += len(json.dumps(frame))
        index += 1
    message = "Ingest canary: synthetic crash, safe to ignore"
    if probes:
        message += " | probe: " + " ".join(probes)
    return {
        "event_id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": "cocoa",
        "level": "error",
        "logger": "ingest-canary",
        "release": release,
        "environment": environment,
        "fingerprint": ["ingest-canary"],
        "tags": {"canary": "true", "canary_payload_kb": str(payload_kb), "canary_probe": str(probe).lower()},
        "message": message,
        "exception": {
            "values": [
                {
                    "type": "IngestCanaryError",
                    "value": message,
                    "mechanism": {"type": "canary", "handled": False},
                    "stacktrace": {"frames": frames},
                }
            ]
        },
    }


def build_envelope(dsn: Dsn, event: dict) -> bytes:
    body = json.dumps(event, separators=(",", ":")).encode()
    header = {"event_id": event["event_id"], "sent_at": datetime.now(timezone.utc).isoformat()}
    item = {"type": "event", "content_type": "application/json", "length": len(body)}
    return b"\n".join([json.dumps(header).encode(), json.dumps(item).encode(), body, b""])


def send_envelope(dsn: Dsn, envelope: bytes, use_gzip: bool, timeout: float) -> tuple[int, str]:
    data = gzip.compress(envelope) if use_gzip else envelope
    headers = {
        "Content-Type": "application/x-sentry-envelope",
        "X-Sentry-Auth": dsn.auth_header,
        "User-Agent": f"ingest-canary/{VERSION}",
    }
    if use_gzip:
        headers["Content-Encoding"] = "gzip"
    req = urllib.request.Request(dsn.envelope_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(200).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(500).decode(errors="replace")
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return 0, str(exc)


def event_arrived(api_url: str, token: str, org: str, project: str, event_id: str, timeout: float) -> bool:
    url = f"{api_url}/api/0/projects/{org}/{project}/events/{event_id}/"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    except (urllib.error.URLError, socket.timeout, OSError):
        return False


class Statsd:
    def __init__(self, addr: str | None, prefix: str = "ingest_canary"):
        self.prefix = prefix
        self.target: tuple[str, int] | None = None
        if addr:
            host, _, port = addr.partition(":")
            self.target = (host, int(port or 8125))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if self.target else None

    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        if not self.sock or not self.target:
            return
        tag_str = ""
        if tags:
            tag_str = "|#" + ",".join(f"{k}:{v}" for k, v in tags.items())
        self.sock.sendto(f"{self.prefix}.{name}:{value}|g{tag_str}".encode(), self.target)


def log(record: dict) -> None:
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    print(json.dumps(record, ensure_ascii=False), flush=True)


def run_once(cfg: dict, statsd: Statsd, use_gzip: bool) -> dict:
    dsn: Dsn = cfg["dsn"]
    event_id = uuid.uuid4().hex
    event = build_event(event_id, cfg["payload_kb"], cfg["probe"], cfg["release"], cfg["environment"])
    envelope = build_envelope(dsn, event)
    started = time.monotonic()
    status, body = send_envelope(dsn, envelope, use_gzip, cfg["http_timeout"])
    send_ms = round((time.monotonic() - started) * 1000)

    result = {
        "event_id": event_id,
        "gzip": use_gzip,
        "payload_bytes": len(envelope),
        "probe": cfg["probe"],
        "http_status": status,
        "send_ms": send_ms,
        "delivered": False,
        "verify_ms": None,
    }
    if status != 200:
        result["error"] = body.strip()[:300] or "no response"
        result["stage"] = "ingest"  # never made it past WAF/proxy/relay
    elif not cfg["token"]:
        result["stage"] = "unverified"
    else:
        deadline = time.monotonic() + cfg["verify_timeout"]
        while time.monotonic() < deadline:
            if event_arrived(cfg["api_url"], cfg["token"], cfg["org"], cfg["project"], event_id, cfg["http_timeout"]):
                result["delivered"] = True
                result["verify_ms"] = round((time.monotonic() - started) * 1000)
                break
            time.sleep(cfg["poll_interval"])
        result["stage"] = "delivered" if result["delivered"] else "accepted_not_stored"

    tags = {"gzip": str(use_gzip).lower(), "probe": str(cfg["probe"]).lower()}
    statsd.gauge("accepted", 1 if status == 200 else 0, tags)
    statsd.gauge("delivered", 1 if result["delivered"] else 0, tags)
    statsd.gauge("send_ms", send_ms, tags)
    if result["verify_ms"] is not None:
        statsd.gauge("verify_ms", result["verify_ms"], tags)
    return result


def load_config() -> dict:
    env = os.environ.get
    raw_dsn = env("INGEST_CANARY_DSN", "")
    if not raw_dsn:
        raise SystemExit("INGEST_CANARY_DSN is not set; create a 'canary' project in Sentry and use its public DSN")
    return {
        "dsn": Dsn.parse(raw_dsn),
        "api_url": env("INGEST_CANARY_API_URL", "http://web:9000").rstrip("/"),
        "token": env("INGEST_CANARY_TOKEN") or env("SYMBOL_PORTAL_SENTRY_TOKEN") or "",
        "org": env("INGEST_CANARY_ORG") or env("SYMBOL_PORTAL_SENTRY_ORG") or "sentry",
        "project": env("INGEST_CANARY_PROJECT", "canary"),
        "interval": float(env("INGEST_CANARY_INTERVAL", "300")),
        "payload_kb": int(env("INGEST_CANARY_PAYLOAD_KB", "256")),
        "probe": env("INGEST_CANARY_PROBE_STRINGS", "0") in ("1", "true", "yes"),
        "also_plain": env("INGEST_CANARY_ALSO_PLAIN", "1") in ("1", "true", "yes"),
        "verify_timeout": float(env("INGEST_CANARY_VERIFY_TIMEOUT", "120")),
        "poll_interval": float(env("INGEST_CANARY_POLL_INTERVAL", "3")),
        "http_timeout": float(env("INGEST_CANARY_HTTP_TIMEOUT", "30")),
        "release": env("INGEST_CANARY_RELEASE", f"ingest-canary@{VERSION}"),
        "environment": env("INGEST_CANARY_ENVIRONMENT", "canary"),
        "health_file": env("INGEST_CANARY_HEALTH_FILE", "/tmp/health.txt"),
        "state_file": env("INGEST_CANARY_STATE_FILE", "/tmp/last-result.json"),
        "statsd": env("STATSD_ADDR") or None,
    }


def main() -> int:
    cfg = load_config()
    statsd = Statsd(cfg["statsd"])
    once = "--once" in sys.argv
    log({"msg": "ingest canary starting", "envelope_url": cfg["dsn"].envelope_url, "interval": cfg["interval"],
         "payload_kb": cfg["payload_kb"], "probe": cfg["probe"], "verify": bool(cfg["token"])})
    while True:
        results = [run_once(cfg, statsd, use_gzip=True)]
        if cfg["also_plain"]:
            results.append(run_once(cfg, statsd, use_gzip=False))
        for result in results:
            log({"msg": "canary result", **result})
        ok = all(r["stage"] in ("delivered", "unverified") for r in results)
        with open(cfg["state_file"], "w") as fh:
            json.dump({"ok": ok, "results": results, "ts": datetime.now(timezone.utc).isoformat()}, fh)
        with open(cfg["health_file"], "w") as fh:
            fh.write("ok\n")
        if once:
            return 0 if ok else 1
        # Jitter so several canaries never line up on the same second.
        time.sleep(cfg["interval"] + random.uniform(0, min(30, cfg["interval"] / 10)))


if __name__ == "__main__":
    sys.exit(main())
