# Ingest edge (DMZ Relay)

The meeting notes describe stack traces being blocked between the phone and
Countly by the WAF and Log4j filters, and a team that no longer trusts the
numbers because of it. This package moves crash ingest onto its own hostname
and path, terminates TLS in the DMZ, and puts a Sentry Relay in **proxy mode**
in front of the internal stack.

```
phone ── https://crash-ingest.example.com/api/<id>/envelope/ ──> [DMZ] nginx ─> Relay (proxy, disk spool)
                                                                                    │ one outbound HTTPS
                                                                     [internal] nginx ─> Relay (processing) ─> Kafka ─> Sentry
```

Why it fixes the trust problem:

- **One narrow surface for the WAF.** Only `/api/<project id>/envelope/` exists
  on this host. The WAF policy for this hostname can be "size and rate limits
  only, no content signatures": Relay already authenticates every request with
  the DSN key and rejects malformed envelopes, and the SDK bodies are gzip so
  signature matching on them is pointless anyway.
- **Nothing is lost while the backend is down.** Relay spools envelopes to disk
  (`spool.envelopes`, 5 GB by default) and drains when the internal Sentry is
  back. Countly dropped them.
- **Measurable.** Point the [ingest canary](../ingest-canary/README.md) at this
  hostname; the `ingest` stage in its results is exactly "blocked at the edge".
  Compare the edge nginx access log (`/api/.../envelope/` 200s) with the
  internal Relay's accepted outcomes in Sentry › Stats for a per-day loss figure.

## Deploy (DMZ host)

```bash
cp ingest-edge/.env.example ingest-edge/.env         # EDGE_HOSTNAME, RELAY_IMAGE
cp ingest-edge/relay/config.example.yml ingest-edge/relay/config.yml
#   relay.upstream -> the internal Sentry URL (its nginx), reachable from the DMZ
cp fullchain.pem privkey.pem ingest-edge/certs/
cd ingest-edge && docker compose up -d
curl -s https://crash-ingest.example.com/api/relay/healthcheck/ready/
```

Firewall: allow DMZ host → internal Sentry nginx (443/80) only. No inbound
from the internal network is required. Proxy mode needs no Relay registration
in the Sentry UI.

Pin `RELAY_IMAGE` to the same version as `RELAY_IMAGE` in the main `.env`.

## App side

Change the DSN host only, keep key and project id:

```
https://<key>@sentry.example.com/<id>   ->   https://<key>@crash-ingest.example.com/<id>
```

Keep the old hostname routable during the rollout; older app versions in the
field will use it for months.

## WAF checklist for `crash-ingest.example.com`

| Rule                                  | Setting                                                          |
|---------------------------------------|------------------------------------------------------------------|
| Allowed paths                         | `/api/[0-9]+/envelope/` (and `/store/` if legacy SDKs remain)    |
| Methods                               | `POST` (plus `OPTIONS` if browser SDKs share the host)           |
| Body size                             | ≥ 20 MB (Relay caps at 100 MB; screenshots/attachments are big)   |
| Content-Type                          | `application/x-sentry-envelope`, `application/octet-stream`      |
| Content-Encoding                      | allow `gzip`, `br`, `deflate`                                    |
| Signature rules (Log4Shell, SQLi, XSS)| **off** for this host: bodies are crash data, not user requests  |
| Rate limiting                         | per source IP, generous (a crash loop can retry every few seconds)|
| Logging                               | log every non-2xx with the `X-Sentry-Auth` public key for triage  |

## Verify

1. `ingest-canary` with `INGEST_CANARY_DSN` on this hostname reports `delivered`.
2. `INGEST_CANARY_PROBE_STRINGS=1` also reports `delivered`: signature rules are off.
3. Stop the internal Sentry for a minute: canary shows `accepted_not_stored`,
   the edge Relay logs spooling, and the events appear after Sentry returns.
