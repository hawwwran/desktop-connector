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

    /**
     * Reset cumulative batterystats counters so the next `--charged` dump
     * has a window that lines up with `app.log` being cleared in the same
     * tick. Requires the same DUMP permission `capture` uses; on consumer
     * devices that's granted with
     *
     *   adb shell pm grant com.desktopconnector android.permission.DUMP
     *
     * The system command prints "Battery stats reset." on success — we
     * report that to the caller so the UI can confirm or show a fallback
     * message.
     */
    fun reset(): Boolean {
        return try {
            val proc = ProcessBuilder("dumpsys", "batterystats", "--reset")
                .redirectErrorStream(true)
                .start()
            val text = proc.inputStream.bufferedReader().readText()
            if (!proc.waitFor(5, TimeUnit.SECONDS)) {
                proc.destroy()
                return false
            }
            text.contains("Battery stats reset.")
        } catch (_: Exception) {
            false
        }
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
