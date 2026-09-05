# Mobile crash analytics on self-hosted Sentry

This fork packages self-hosted Sentry as a replacement for Countly Crash
Analytics for iOS and Android. Upstream `docker-compose.yml` is untouched; the
additions live in their own directories and are wired in through
`docker-compose.symbol-portal.yml` (enabled by `COMPOSE_FILE` in `.env`).

| Countly pain point (meeting notes)                         | Where it is solved                                                                                     |
|------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| Converting dSYM zips to "dwarf" by hand, uploading per ID   | [`symbol-portal/`](symbol-portal/README.md): drop the zip, portal unpacks and uploads; [`mobile-ci/`](mobile-ci/README.md) makes CI do it before release |
| No view of which mapping files are missing                  | Symbol Portal › *Eksik Semboller* tab                                                                  |
| OS frames (UIKit, libsystem, libart) never symbolicated     | [`symbol-server/`](symbol-server/README.md) + `SENTRY_BUILTIN_SOURCES` block in `sentry/sentry.conf.example.py` |
| Dashboard too thin, 5-second auto-refresh                   | Sentry Issues / Discover / Dashboards; real-time updates are an opt-in toggle                          |
| Stack traces blocked by WAF, "do we see 100%?"              | [`ingest-canary/`](ingest-canary/README.md) measures delivery through the public path; Release Health sessions give an independent crash count |

## Roadmap status

- [x] Faz 2: Symbol Portal (drag-and-drop upload, missing-symbol report, uploaded files)
- [x] Faz 2: CI templates (Fastlane, Xcode build phase, Gradle plugin, GitHub Actions, GitLab CI)
- [x] Faz 2: internal symbol server for iOS/Android OS symbols
- [x] Faz 1: ingest canary with gzip/plain and WAF-probe modes
- [ ] Faz 0: pin images to a CalVer release, HTTPS, mail, S3 filestore, backups, statsd
- [ ] Faz 1: dedicated ingest hostname and WAF policy, DMZ Relay in proxy mode
- [ ] Faz 3: Teams/Slack/Jira integrations, alert rules, mobile dashboard template
- [ ] Faz 4: parallel run with Countly, grouping calibration, cut-over

## Operating notes

- Symbols apply to new crashes only. Sentry removed reprocessing, so the CI gate
  (`sentry-cli debug-files check`) is the safety net, the portal the exception path.
- The portal and canary share one org auth token (`SYMBOL_PORTAL_SENTRY_TOKEN`)
  with `project:read`, `project:write`, `event:read`, `org:read`. Put `/symbols/`
  behind the same SSO/VPN as Sentry.
- `errors-only` profile disables the metrics consumers, which Release Health
  (crash-free sessions/users) needs. Keep `feature-complete` for that metric.
- Existing installs do not pick up changes to `*.example.*` files automatically:
  copy the `connect_to_reserved_ips` line into `symbolicator/config.yml` and the
  `SENTRY_BUILTIN_SOURCES` block into `sentry/sentry.conf.py` by hand.
