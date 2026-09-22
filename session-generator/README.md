# Session generator (demo data for Release Health)

Sends realistic session traffic for `android-app` and `ios-app` so the Releases
page shows **version adoption**, **crash-free sessions** and **crash-free users**
without a fleet of phones. Demo servers only.

The story it tells, per app:

| Release | Shipped | Crash rate |
|---|---|---|
| 8.18.3 | 30 days ago | 0.6 % |
| 8.19.0 | 16 days ago | 0.9 % |
| 8.19.1 | 9 days ago | 0.5 % |
| 8.20.0 | 4 days ago | **3.5 %** (regression) |
| 8.20.1 | 1.5 days ago | 0.4 % (fix) |

Each simulated user upgrades after a personal delay (mean 2.2 days), so the newest
release's adoption climbs day by day. Traffic follows a Turkey day/night cycle.

Relay accepts sessions up to about five days old, so the first start backfills
the last 108 hours; after that it sends the minutes since the previous cycle every
10 minutes. State (the story's anchor time and the last window sent) lives in the
`sentry-session-generator` volume, so restarts neither duplicate nor lose data.
Delete that volume to start a fresh story.

Requirements: the `feature-complete` profile (the metrics consumers turn sessions
into Release Health data) and `SYMBOL_PORTAL_SENTRY_TOKEN` (used to look up the
DSNs and create missing releases).

```bash
# .env
COMPOSE_PROFILES=feature-complete,ingest-canary,session-generator
docker compose up -d --build session-generator
docker compose logs -f session-generator
```
