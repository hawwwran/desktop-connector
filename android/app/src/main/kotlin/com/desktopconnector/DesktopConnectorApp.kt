package com.desktopconnector

import android.app.Application
import com.desktopconnector.data.AppLog
import com.desktopconnector.data.MultiPairMigrationRunner
import com.desktopconnector.service.PollService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

class DesktopConnectorApp : Application() {
    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        Security.removeProvider(BouncyCastleProvider.PROVIDER_NAME)
        Security.addProvider(BouncyCastleProvider())

        AppLog.init(this)
        AppLog.log("App", "Started")

        // One-shot multi-pair cleanup. Idempotent across restarts via
        // AppPreferences.multiPairMigrationDone. Runs off the main thread —
        // touches Room (which forbids main-thread queries) and KeyManager.
        appScope.launch { MultiPairMigrationRunner.runIfNeeded(this@DesktopConnectorApp) }

        PollService.start(this)
    }
}
