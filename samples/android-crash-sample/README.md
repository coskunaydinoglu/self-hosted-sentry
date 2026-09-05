# Android crash sample

A two-screen Android app used to exercise the crash pipeline end to end against
this self-hosted Sentry: R8-obfuscated release build, Sentry Android SDK with
ANR and session tracking, and the Sentry Android Gradle Plugin uploading
`mapping.txt` with the UUID it injects into the APK.

## Setup

```bash
cp sentry.properties.example sentry.properties   # url, org, project, token
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
```

The DSN in `AndroidManifest.xml` points at `10.0.2.2:9000`, which is the host
machine as seen from an emulator. For a physical phone use the host's LAN IP.

## Two ways to test

**A. The right way (plugin uploads mapping during the build):**

```bash
./gradlew assembleRelease
adb install -r app/build/outputs/apk/release/app-release.apk
adb shell am start -n com.example.crashsample/.MainActivity --es action crash
adb shell am start -n com.example.crashsample/.MainActivity      # relaunch: SDK sends the cached crash
```

The issue in Sentry shows `OrderProcessor.discountFor` with Kotlin line numbers.

**B. The Countly situation (mapping not uploaded, fix through the portal):**

```bash
./gradlew assembleRelease -PsentryAutoUpload=false
```

Install, crash, relaunch. The issue shows `a.b.c.a(SourceFile:12)` and the
portal's *Eksik Semboller* tab lists a `proguard` entry with the UUID. Find the
UUID the plugin baked into the APK and upload the mapping with it:

```bash
unzip -p app/build/outputs/apk/release/app-release.apk assets/sentry-debug-meta.properties
# io.sentry.ProguardUuids=<uuid>
```

Drop `app/build/outputs/mapping/release/mapping.txt` on the portal with that UUID
in the ProGuard field. The next crash is readable; the old one stays obfuscated,
which is why the plugin/CI path is the default.

`--es action` accepts `crash`, `npe`, `handled`, `anr`; the buttons do the same by hand.
