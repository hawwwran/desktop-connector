import groovy.json.JsonSlurper

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
}

// Single source of truth for the Android version lives at the repo root:
//   version.json -> { "android": "X.Y.Z", ... }
// Fall back to a stamped-in value if the file is missing (e.g. custom
// source drops that don't include the file).
val androidVersionName: String = run {
    val f = rootProject.file("../version.json")
    if (f.isFile) {
        @Suppress("UNCHECKED_CAST")
        (JsonSlurper().parse(f) as? Map<String, Any?>)
            ?.get("android") as? String ?: "0.0.0"
    } else "0.0.0"
}

android {
    namespace = "com.desktopconnector"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.desktopconnector"
        minSdk = 26
        targetSdk = 35
        versionCode = 4
        versionName = androidVersionName
        setProperty("archivesBaseName", "Desktop-Connector-${versionName}")
    }

    signingConfigs {
        create("release") {
            val ks = rootProject.file("keystore.jks")
            if (ks.exists()) {
                storeFile = ks
                storePassword = "desktopconnector"
                keyAlias = "desktop-connector"
                keyPassword = "desktopconnector"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    lint {
        abortOnError = false
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }
}

dependencies {
    // Compose
    val composeBom = platform("androidx.compose:compose-bom:2024.10.00")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.navigation:navigation-compose:2.7.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.2")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.2")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // Crypto - Bouncy Castle for X25519 + HKDF
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")

    // Encrypted storage
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Networking - OkHttp
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // QR scanning - CameraX + ML Kit
    implementation("com.google.mlkit:barcode-scanning:17.2.0")
    implementation("androidx.camera:camera-camera2:1.3.3")
    implementation("androidx.camera:camera-lifecycle:1.3.3")
    implementation("androidx.camera:camera-view:1.3.3")

    // Background work
    implementation("androidx.work:work-runtime-ktx:2.9.0")

    // Local database - Room
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // Firebase (dynamic init — no google-services plugin)
    implementation(platform("com.google.firebase:firebase-bom:33.7.0"))
    implementation("com.google.firebase:firebase-messaging")

    // Core
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.core:core-splashscreen:1.0.1")
    implementation("androidx.fragment:fragment-ktx:1.8.5")

    // Unit tests (JVM, no emulator). Added for D.4a UploadStreamLoopTest.
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
}
