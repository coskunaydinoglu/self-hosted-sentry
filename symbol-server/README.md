# Internal symbol server (iOS / Android OS symbols)

Sentry SaaS resolves frames inside UIKit, libsystem, libdispatch, libart and
friends from symbol buckets that self-hosted installs cannot reach. Without
them the app's own frames are fine but OS frames stay as raw addresses, which
hides the real cause of many crashes (main-thread checker, dispatch queues,
UIKit assertions).

This directory adds a static HTTP symbol server on the compose network and
registers it as a built-in repository in Sentry.

## Setup

1. **Sort symbols on a Mac** that has connected devices running the iOS versions
   you ship to (Xcode copies their OS symbols to `~/Library/Developer/Xcode/iOS DeviceSupport`):

   ```bash
   ./symbol-server/import-ios-symbols.sh ./sorted user@sentry-host:/opt/sentry/self-hosted/symbol-server/data/
   ```

   Repeat whenever a new iOS version appears in the field. The script is
   idempotent; symsorter only adds new debug IDs.

2. **Symbolicator must be allowed to talk to internal hosts.** New installs get
   this from `symbolicator/config.example.yml`; on an existing install add to
   `symbolicator/config.yml`:

   ```yaml
   connect_to_reserved_ips: true
   ```

   and restart: `docker compose restart symbolicator`.

3. **Sentry must know the source.** New installs get it from
   `sentry/sentry.conf.example.py` (the `SENTRY_BUILTIN_SOURCES["internal-system-symbols"]`
   block). On an existing install copy that block into `sentry/sentry.conf.py`
   and restart the Sentry containers.

4. **Enable it per project**: Project Settings › Debug Files › Built-in
   Repositories › add *Internal system symbols*. New crashes from that point on
   resolve OS frames.

## Android

The same server works for Android NDK crashes: sort `libc.so`, `libart.so`,
`libandroid_runtime.so` and vendor libraries from the device images you care
about with `symsorter -zz --bundle-id android_<version> -o data/ <dir>` and
they are picked up through the same source (`elf_code`/`elf_debug` filters).
Symbols come from Android platform builds or `adb pull /system/lib64` of the
target devices; there is no public source for OEM builds.

## Checking

`curl http://<host>/symbols-server/` is intentionally not exposed; the server
is internal to the compose network. Inside it:

```bash
# the symbolicator image has no shell, so probe from the portal on the same network
docker compose exec symbol-portal python -c "import urllib.request; print(urllib.request.urlopen('http://symbol-server/health').read())"
docker compose exec symbol-server ls /usr/share/nginx/html | head
```

In Sentry, an iOS event's *Images Loaded* section shows OS images turning from
"missing" to "found" once the matching build is served.
