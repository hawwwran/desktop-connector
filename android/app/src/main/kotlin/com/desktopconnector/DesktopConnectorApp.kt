package com.desktopconnector

import android.app.Application
import com.desktopconnector.data.AppLog
import com.desktopconnector.service.PollService
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

class DesktopConnectorApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Security.removeProvider(BouncyCastleProvider.PROVIDER_NAME)
        Security.addProvider(BouncyCastleProvider())

        AppLog.init(this)
        AppLog.log("App", "Started")

        PollService.start(this)
    }
}
