# Symbol Portal

A small companion service for self-hosted Sentry that closes the two gaps mobile
teams coming from Countly hit first:

1. **Drag-and-drop symbol upload.** Drop the dSYM zip Xcode / App Store Connect
   gives you, a `mapping.txt` from R8/ProGuard, or NDK `.so` files. The portal
   unpacks archives, skips the noise (`Info.plist`, `__MACOSX`, `Relative/`),
   and uploads each debug file to Sentry with the same chunked protocol
   `sentry-cli` uses. No local tooling, no manual "convert to DWARF" step.
2. **Missing-symbols report.** Sentry only flags a missing dSYM on the
   individual event. The portal scans recent events per project and lists every
   debug ID Sentry could not resolve, how many crashes it affected, which
   releases, and whether the file has been uploaded since.

3. **On-demand re-symbolication.** Paste an event ID (or click one from the
   missing-symbols report) and the portal resolves its native stack trace
   through the stack's own Symbolicator using the project's debug files and the
   internal OS symbol server. This is Countly's "Symbolicate" button for events
   that were stored before the symbols arrived. The stored event is not modified.

It talks to Sentry over the public REST API only; nothing in Sentry itself is
patched, so upstream image upgrades keep working.

## Enable

1. Create an organization auth token in Sentry (**Settings › Auth Tokens**) with
   `project:read`, `project:write`, `event:read`, `org:read`.
2. Put it in `.env` as `SYMBOL_PORTAL_SENTRY_TOKEN`, and set
   `SYMBOL_PORTAL_SENTRY_ORG` to your organization slug.
3. `.env` already adds `docker-compose.symbol-portal.yml` through `COMPOSE_FILE`.
   Run `./install.sh` (or `docker compose up -d --build symbol-portal nginx`).
4. Open `<your sentry url>/symbols/`.

If you use `.env.custom`, copy the `COMPOSE_FILE` and `SYMBOL_PORTAL_*` lines there too.

Set `SYMBOL_PORTAL_BASIC_AUTH=user:password` for a minimal guard, but the portal
holds a write-capable token, so put it behind the same SSO/VPN as Sentry itself.

## How uploads map to Sentry

| You drop                                   | Portal does                                                                        |
|--------------------------------------------|------------------------------------------------------------------------------------|
| `*.dSYM.zip`, `.xcarchive` zip, zip of zips | Extracts `Contents/Resources/DWARF/*`, chunk-uploads each, polls assemble           |
| raw Mach-O / ELF / Breakpad `.sym`         | Chunk-uploads as is                                                                |
| `mapping.txt` (R8/ProGuard)                | Wraps as `proguard/<uuid>.txt` zip and posts to the debug-files endpoint            |

For ProGuard the UUID must match what the app reports (`io.sentry.proguard-uuid`).
The Sentry Android Gradle Plugin generates and injects it and can upload the
mapping itself; use the portal for the mapping only when that pipeline is not in
place. If you leave the UUID empty the portal derives one from the file content
and tells you to configure the app with it.

Uploaded symbols apply to **new** crashes. Sentry no longer reprocesses old
events, so upload before release; the report exists to catch what slipped and
the re-symbolication tab shows what an old crash would have looked like.

## Development

```bash
cd symbol-portal
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
pytest
SYMBOL_PORTAL_SENTRY_URL=https://sentry.example.com SYMBOL_PORTAL_SENTRY_TOKEN=... uvicorn app.main:app --reload
```

Tests run against an in-memory fake of the Sentry endpoints (`tests/conftest.py`).
