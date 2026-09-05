# Mobile CI templates: symbols before release

Sentry symbolicates at ingest time and no longer reprocesses old events. The
only reliable way to never see a raw stack trace is to make symbol upload a
release gate. These templates do that for iOS and Android against this
self-hosted instance. Use the Symbol Portal (`/symbols/`) for the exceptions.

All of them need two secrets in CI:

| Variable            | Value                                                      |
|---------------------|------------------------------------------------------------|
| `SENTRY_URL`        | `https://<your sentry host>/` (self-hosted, not sentry.io) |
| `SENTRY_AUTH_TOKEN` | An **Organization Token** (Developer Settings › Organization Tokens) is enough here: uploads only |
| `SENTRY_ORG`        | Organization slug                                          |
| `SENTRY_PROJECT`    | Project slug                                               |

## iOS

- `ios/Fastfile.example.rb`: `upload_symbols` lane. Uploads every dSYM in the
  archive (app, extensions, frameworks, pods) with source context, waits for
  Sentry to process them, then runs `debug-files check` on the app binary and
  fails the lane if anything is missing.
- `ios/xcode-build-phase.sh`: the same as an Xcode "Run Script" phase for teams
  without Fastlane. Only runs for Release configurations.
- Bitcode is gone, so no App Store Connect dSYM download step is needed. If you
  distribute through TestFlight, upload from the CI archive, not from ASC.

## Android

- `android/build.gradle.kts.example`: Sentry Android Gradle Plugin config.
  Uploads the R8 mapping with the UUID it injects into the APK, uploads NDK
  symbols (`.so` with debug info from `merged_native_libs`) and source context.
- `android/sentry.properties.example`: where the plugin reads the self-hosted URL.

## Pipelines

- `pipelines/github-actions.yml` and `pipelines/gitlab-ci.yml`: a `symbols`
  job that runs after the build, uploads, and verifies. Copy the job into
  your existing pipeline; the build steps are placeholders.

## Verifying

`sentry-cli debug-files check <binary>` prints the debug id and whether Sentry
already has it. In the Sentry UI: **Project Settings › Debug Files**. In the
portal: **Eksik Semboller** tab shows what recent crashes still miss.
