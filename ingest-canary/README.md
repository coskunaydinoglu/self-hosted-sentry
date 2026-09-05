# Ingest canary

Answers the question the team kept asking about Countly: *are some crashes never
reaching us?* Instead of guessing, the canary sends a synthetic crash report
through the **public** ingest path (WAF, load balancer, nginx, Relay) every few
minutes and then checks over the internal network whether Sentry stored it.

Each cycle sends two envelopes, one gzip-compressed like the mobile SDKs do and
one plain, so a WAF that only inspects readable bodies shows up as a difference
between the two. Every result is a JSON log line with the stage it reached:

| stage                 | meaning                                                              |
|-----------------------|----------------------------------------------------------------------|
| `ingest`              | HTTP error or timeout on the envelope POST: blocked before Relay      |
| `accepted_not_stored` | Relay said 200 but the event never became queryable (pipeline issue)  |
| `delivered`           | Event is stored and visible in the API; `verify_ms` is the latency    |
| `unverified`          | No API token configured, only the POST was checked                   |

Metrics `ingest_canary.accepted`, `ingest_canary.delivered`, `ingest_canary.send_ms`,
`ingest_canary.verify_ms` (tagged `gzip`, `probe`) go to `STATSD_ADDR` when set.
`/tmp/last-result.json` in the container holds the last cycle for scripts.

## Setup

1. Create a Sentry project named `canary` (platform: other). Copy its DSN and
   replace the host with the **public** ingest hostname your apps use.
2. In `.env`: `INGEST_CANARY_DSN=https://<key>@crash-ingest.example.com/<id>`.
   The API check reuses `SYMBOL_PORTAL_SENTRY_TOKEN` (needs `event:read`).
3. `docker compose up -d --build ingest-canary`, then `docker compose logs -f ingest-canary`.

Alert on `delivered == 0` for two consecutive cycles. In Sentry itself, create
an alert on the `canary` project: *no events received in 15 minutes*. That
second alert fires even when this container is down.

## Probing the WAF on purpose

`INGEST_CANARY_PROBE_STRINGS=1` adds strings that trip common WAF signatures
(Log4Shell `${jndi:`, SQLi, XSS, path traversal) to the frames. Real crash
reports contain such text in breadcrumbs, log lines and user input, so if the
probe run fails while the plain one passes you have found the rule that eats
crashes. Coordinate with the security team before enabling it; it looks like an
attack in their dashboards by design.

## One-shot check

```bash
docker compose run --rm ingest-canary python canary.py --once && echo delivered
```

Exit code 1 means at least one envelope did not make it.
