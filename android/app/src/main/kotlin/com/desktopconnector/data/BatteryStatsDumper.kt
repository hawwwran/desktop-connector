package com.desktopconnector.data

import java.util.concurrent.TimeUnit

object BatteryStatsDumper {

    fun capture(pkg: String): String = buildString {
        append("===== dumpsys batterystats --charged ")
        append(pkg)
        append(" =====\n")
        append(run(arrayOf("dumpsys", "batterystats", "--charged", pkg)))
        append("\n===== end dumpsys =====\n\n")
    }

    private fun run(cmd: Array<String>): String {
        return try {
            val proc = ProcessBuilder(*cmd).redirectErrorStream(true).start()
            val text = proc.inputStream.bufferedReader().readText()
            if (!proc.waitFor(10, TimeUnit.SECONDS)) {
                proc.destroy()
                return text + "\n(dumpsys timed out after 10s)"
            }
            text.ifBlank {
                "(no output — grant DUMP permission with:\n" +
                    "  adb shell pm grant com.desktopconnector android.permission.DUMP)"
            }
        } catch (e: Exception) {
            "(exec failed: ${e.javaClass.simpleName}: ${e.message})"
        }
    }
}
