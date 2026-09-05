plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("io.sentry.android.gradle")
}

android {
    namespace = "com.example.crashsample"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.crashsample"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0.0"
    }

    buildTypes {
        release {
            // R8 on: class and method names are obfuscated, so a mapping file is
            // required to read the stack trace. This is what we are testing.
            isMinifyEnabled = true
            isShrinkResources = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            // Sign with the debug key so the release APK installs on an emulator.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

sentry {
    // URL, org, project and token come from sentry.properties next to this project
    // (see sentry.properties.example) or from SENTRY_URL / SENTRY_AUTH_TOKEN env vars.
    includeProguardMapping.set(true)
    autoUploadProguardMapping.set((project.findProperty("sentryAutoUpload") as String? ?: "true").toBoolean())
    // No NDK code in this sample.
    uploadNativeSymbols.set(false)
    // Source context: the stack trace in Sentry shows the Kotlin lines around the crash.
    includeSourceContext.set(true)
    // Crash analytics only for now.
    tracingInstrumentation { enabled.set(false) }
    autoInstallation {
        enabled.set(true)
        sentryVersion.set("8.15.0")
    }
    ignoredBuildTypes.set(setOf("debug"))
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    // io.sentry:sentry-android is added by the plugin's autoInstallation.
}
